# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import requests
import urlparse

from perfkitbenchmarker import disk
from perfkitbenchmarker import flags
from perfkitbenchmarker import virtual_machine, linux_virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.mesos.mesos_disk import LocalDisk

FLAGS = flags.FLAGS

MARATHON_API_PREFIX = '/v2/apps/'
USERNAME = 'root'


class MesosDockerInstance(virtual_machine.BaseVirtualMachine):
  """
  Represents a Docker instance spawned by Marathon framework on a Mesos cluster
  """

  CLOUD = 'Mesos'

  def __init__(self, vm_spec):
    super(MesosDockerInstance, self).__init__(vm_spec)
    self.user_name = USERNAME
    self.api_url = urlparse.urljoin(FLAGS.marathon_address, MARATHON_API_PREFIX)
    self.app_url = urlparse.urljoin(self.api_url, self.name)

  def _CreateDependencies(self):
    self._CheckPrerequisites()
    self._CreateVolumes()

  def _Create(self):
    self._CreateApp()
    self._WaitForBootCompletion()

  def _PostCreate(self):
    self._SetupSSH()
    self._ConfigureProxy()

  def _Delete(self):
    self._DeleteApp()

  def _CheckPrerequisites(self):
    """
    Exits if any of the prerequisites is not met.
    """
    if self.disk_specs and self.disk_specs[0].disk_type == disk.STANDARD:
      raise Exception('Currently only local disks are supported. Please '
                      're-run the benchmark with "--scratch_disk_type=local".')
    if not FLAGS.marathon_address:
      raise Exception('Please provide the address and port of Marathon '
                      'framework. Example: 10:20:30:40:8080')

  def _CreateVolumes(self):
    """
    Creates volumes for scratch disks.
    """
    for disk_num, disk_spec in enumerate(self.disk_specs):
      if disk_spec.disk_type == disk.LOCAL:
        scratch_disk = LocalDisk(disk_num, disk_spec, self.name)
      else:
        # TODO: support for Ceph
        pass
      scratch_disk._Create()
      self.scratch_disks.append(scratch_disk)

  def _CreateApp(self):
    """
    Creates Marathon's App (Docker instance).
    """
    logging.info("Attempting to create App: %s" % self.name)
    body = self._BuildAppBody()
    headers = {'content-type': 'application/json'}
    output = requests.post(self.api_url, data=body, headers=headers)
    if output.status_code != requests.codes.CREATED:
      raise Exception("Unable to create App: %s" % output.text)
    logging.info("App %s created successfully." % self.name)

  @vm_util.Retry(poll_interval=10, max_retries=600, log_errors=False)
  def _WaitForBootCompletion(self):
    """
    Periodically asks Marathon if the instance is already running.
    """
    logging.info("Waiting for App %s to get up and running. It may take a while"
                 " if a Docker image is being downloaded for the first time."
                 % self.name)
    output = requests.get(self.app_url)
    output = json.loads(output.text)
    tasks_running = output['app']['tasksRunning']
    if not tasks_running:
      raise Exception("Container is not booted yet. Retrying.")

  @vm_util.Retry(poll_interval=10, max_retries=100, log_errors=True)
  def _SetupSSH(self):
    """
    Setup SSH connection details for each instance:
    - IP address of the instance is the address of a host which instance
    is running on,
    - SSH port is drawn by Marathon and is unique for each instance.
    """
    output = requests.get(self.app_url)
    output = json.loads(output.text)
    tasks = output['app']['tasks']
    if not tasks or not tasks[0]['ports']:
      raise Exception("Unable to figure out where the container is running."
                      "Retrying to retrieve host and port.")
    self.ip_address = tasks[0]['host']
    self.ssh_port = tasks[0]['ports'][0]
    internal_ip, _ = self.RemoteCommand("ifconfig eth0 | grep 'inet addr' | awk"
                                        " -F: '{print $2}' | awk '{print $1}'")
    self.internal_ip = internal_ip.rstrip()

  @vm_util.Retry(poll_interval=10, max_retries=100, log_errors=True)
  def _ConfigureProxy(self):
    """
    In Docker containers environment variables from /etc/environment
    are not sourced - this results in connection problems when running
    behind proxy. Prepending proxy environment variables to bashrc
    solves the problem. Note: APPENDING to bashrc will not work because
    the script exits when it is NOT executed in interactive shell.
    """
    if FLAGS.http_proxy:
      http_proxy = "sed -i '1i export http_proxy=%s' /etc/bash.bashrc"
      self.RemoteCommand(http_proxy % FLAGS.http_proxy)
    if FLAGS.https_proxy:
      https_proxy = "sed -i '1i export https_proxy=%s' /etc/bash.bashrc"
      self.RemoteCommand(https_proxy % FLAGS.http_proxy)
    if FLAGS.ftp_proxy:
      ftp_proxy = "sed -i '1i export ftp_proxy=%s' /etc/bash.bashrc"
      self.RemoteCommand(ftp_proxy % FLAGS.ftp_proxy)

  @vm_util.Retry(poll_interval=10, max_retries=100, log_errors=True)
  def _DeleteApp(self):
    """
    Deletes an App.
    """
    logging.info('Attempting to delete App: %s' % self.name)
    output = requests.delete(self.app_url)
    if output.status_code == requests.codes.NOT_FOUND:
      logging.info('App %s has been already deleted.')
      return
    if output.status_code != requests.codes.OK:
      raise Exception("Deleting App: %s failed. Reattempting." % self.name)

  def _BuildAppBody(self):
    """
    Builds JSON which will be passed as a body of POST request to Marathon
    API in order to create App.
    """
    cat_cmd = ['cat', vm_util.GetPublicKeyPath()]
    key_file, _ = vm_util.IssueRetryableCommand(cat_cmd)
    cmd = "/bin/mkdir /root/.ssh; echo '%s' >> /root/.ssh/authorized_keys; " \
          "/usr/sbin/sshd -D" % key_file
    body = {
        'id': self.name,
        'mem': FLAGS.docker_memory_mb,
        'cpus': FLAGS.docker_cpus,
        'cmd': cmd,
        'container': {
            'type': 'DOCKER',
            'docker': {
                'image': self.image,
                'network': 'BRIDGE',
                'portMappings': [
                    {
                        'containerPort': 22,
                        'hostPort': 0,
                        'protocol': 'tcp'
                    }
                ],
                'privileged': FLAGS.mesos_docker_in_privileged_mode,
                'parameters': []
            }
        }
    }

    for scratch_disk in self.scratch_disks:
      scratch_disk.AttachVolumeInfo(body['container'])

    return json.dumps(body)

  def SetupLocalDisks(self):
    # Do not call parent's method
    return


class DebianBasedMesosDockerInstance(MesosDockerInstance,
                                     linux_virtual_machine.DebianMixin):
  pass
