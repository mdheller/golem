import ipaddress
import logging
import os
import subprocess
from abc import ABCMeta
from contextlib import contextmanager
from typing import Optional, ClassVar, Any, List, Tuple

from golem.docker.client import local_client
from golem.docker.commands.docker_machine import DockerMachineCommandHandler
from golem.docker.config import DOCKER_VM_NAME, GetConfigFunction, \
    CONSTRAINT_KEYS
from golem.docker.hypervisor import Hypervisor
from golem.report import Component, report_calls

logger = logging.getLogger(__name__)


class DockerMachineHypervisor(Hypervisor, metaclass=ABCMeta):

    COMMAND_HANDLER = DockerMachineCommandHandler
    DRIVER_PARAM_NAME = "--driver"
    DRIVER_NAME: ClassVar[str]

    REGENERATE_CERTIFICATES_TIMEOUT = 120.0  # sec
    RESTART_VM_TIMEOUT = 120.0  # sec

    def __init__(self,
                 get_config_fn: GetConfigFunction,
                 vm_name: str = DOCKER_VM_NAME) -> None:
        super().__init__(get_config_fn, vm_name)
        self._config_dir = None

    def setup(self) -> None:
        if self._vm_name not in self.vms:
            if not self.create(self._vm_name, **self._get_config()):
                self._failed_to_create()
                raise Exception('Docker: No vm available and failed to create')

        if not self.vm_running():
            self.restore_vm()
        self._set_env()

    def _failed_to_create(self, vm_name: Optional[str] = None):
        name = vm_name or self._vm_name
        logger.warning('%s: Vm (%s) not found and create failed',
                       self.DRIVER_NAME, name)

    # pylint: disable=unused-argument
    def _parse_create_params(self, **params: Any) -> List[str]:
        return [self.DRIVER_PARAM_NAME, self.DRIVER_NAME]

    @report_calls(Component.hypervisor, 'vm.create')
    def create(self, vm_name: Optional[str] = None, **params) -> bool:
        vm_name = vm_name or self._vm_name
        constraints = {
            k: params.pop(v, None)
            for k, v in CONSTRAINT_KEYS.items()
        }
        command_args = self._parse_create_params(**constraints, **params)

        logger.info('%s: creating VM "%s"', self.DRIVER_NAME, vm_name)

        try:
            self.command('create', vm_name, args=command_args)
            return True
        except subprocess.CalledProcessError as exc:
            out = exc.stdout.decode('utf8') if exc.stdout is not None else ''
            logger.error(
                f'{self.DRIVER_NAME}: error creating VM "{vm_name}"" '
                f'stdout="{out}"')
            return False

    @contextmanager
    @report_calls(Component.hypervisor, 'vm.restart')
    def restart_ctx(self, name: Optional[str] = None):
        with super().restart_ctx(name) as res:
            yield res
        self._set_env()

    @property
    def vms(self):
        try:
            # DON'T use the '-q' option. It doesn't list VMs in invalid state
            output = self.command('list')
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to list VMs: %r", e)
        else:
            if output:
                # Skip first line (header) and last (empty)
                lines = output.split('\n')[1:-1]
                # Get the first word of each line
                return [l.strip().split()[0] for l in lines]
        return []

    def get_port_mapping(self, container_id: str, port: int) -> Tuple[str, int]:
        api_client = local_client()
        c_config = api_client.inspect_container(container_id)
        port = int(
            c_config['NetworkSettings']['Ports'][f'{port}/tcp'][0]['HostPort'])
        raw_ip = self.command('ip', self._vm_name)
        ip = None
        assert isinstance(raw_ip, str)
        for line in raw_ip.splitlines():
            if line and line[0].isnumeric():
                try:
                    ipaddress.ip_address(line)
                    ip = line
                    break
                except ValueError:
                    pass
        assert isinstance(ip, str)
        return ip, port

    @property
    def config_dir(self):
        return self._config_dir

    @report_calls(Component.docker, 'instance.env')
    def _set_env(self, retried=False):
        try:
            output = self.command('env', self._vm_name,
                                  args=('--shell', 'cmd'))
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to update env for VM: %s", e)
            logger.debug("DockerMachine_output: %s", e.output)
            if not retried:
                return self._recover()
            typical_solution_s = \
                """It seems there is a  problem with your Docker installation.
Ensure that you try the following before reporting an issue:

 1. The virtualization of Intel VT-x/EPT or AMD-V/RVI is enabled in BIOS
    or virtual machine settings.
 2. The windows feature 'Hyper-V' is enabled
 3. docker-machine is in your path `docker-machine --version` in ps or cmd
 4. docker-machine ls has no errors `docker-machine  ls` in ps or cmd"""
            logger.error(typical_solution_s)
            raise

        if output:
            self._set_env_from_output(output)
            logger.info('DockerMachine: env updated')
        else:
            logger.warning('DockerMachine: env update failed')

    def _recover(self, restarted=False):
        def _log_warning(msg, e):
            logger.warning(
                "Failed to regenerate certificates: %s -- %s",
                e,
                e.output
            )

        try:
            self.command(
                'regenerate_certs', self._vm_name,
                timeout=self.REGENERATE_CERTIFICATES_TIMEOUT)
        except subprocess.CalledProcessError as e:
            _log_warning("Failed to regenerate certificates", e)
        except subprocess.TimeoutExpired as e:
            _log_warning("Timeout in regenerate certificates", e)
        else:
            return self._set_env(retried=True)

        if not restarted:
            try:
                self.command(
                    'restart', self._vm_name, timeout=self.RESTART_VM_TIMEOUT)
            except subprocess.CalledProcessError as e:
                _log_warning("Failed to restart the VM", e)
            except subprocess.TimeoutExpired as e:
                _log_warning("Timeout in restart the VM", e)
            else:
                return self._recover(restarted=True)

        try:
            if self.remove(self._vm_name):
                self.create(self._vm_name)
        except subprocess.CalledProcessError as e:
            logger.warning("DockerMachine:"
                           " failed to re-create the VM: %s -- %s",
                           e, e.output)

        return self._set_env(retried=True)

    def _set_env_from_output(self, output):
        for line in output.split('\n'):
            if not line:
                continue

            cmd, params = line.split(' ', 1)
            if cmd.lower() != 'set':
                continue

            var, val = params.split('=', 1)
            os.environ[var] = val

            if var == 'DOCKER_CERT_PATH':
                split = val.replace('"', '').split(os.path.sep)
                self._config_dir = os.path.sep.join(split[:-1])
