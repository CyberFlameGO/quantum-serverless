# This code is a Qiskit project.
#
# (C) Copyright IBM 2022.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""
==================================================
Provider (:mod:`quantum_serverless.core.provider`)
==================================================

.. currentmodule:: quantum_serverless.core.provider

Quantum serverless provider
===========================

.. autosummary::
    :toctree: ../stubs/

    ComputeResource
    Provider
"""
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict
from uuid import uuid4

import ray
from ray.dashboard.modules.job.sdk import JobSubmissionClient

from quantum_serverless.core.constrants import OT_PROGRAM_NAME

from quantum_serverless.core.tracing import _trace_env_vars
from quantum_serverless.core.job import Job
from quantum_serverless.core.program import Program
from quantum_serverless.exception import QuantumServerlessException
from quantum_serverless.utils import JsonSerializable


@dataclass
class ComputeResource:
    """ComputeResource class.

    Args:
        name: name of compute_resource
        host: host address of compute_resource
        port_interactive: port of compute_resource for interactive mode
        port_job_server: port of compute resource for job server
        resources: list of resources
    """

    name: str
    host: Optional[str] = None
    port_interactive: int = 10001
    port_job_server: int = 8265
    resources: Optional[Dict[str, float]] = None

    def job_client(self) -> Optional[JobSubmissionClient]:
        """Return job client for given compute resource.

        Returns:
            job client
        """
        if self.host is not None:
            connection_url = f"http://{self.host}:{self.port_job_server}"
            client = None
            try:
                client = JobSubmissionClient(connection_url)
            except ConnectionError:
                logging.warning(
                    "Failed to establish connection with jobs server at %s. "
                    "You will not be able to run jobs on this provider.",
                    connection_url,
                )
            return client
        return None

    def context(self, **kwargs):
        """Returns context allocated for this compute_resource."""
        _trace_env_vars({}, location="on context allocation")

        init_args = {
            **kwargs,
            **{
                "address": kwargs.get(
                    "address",
                    self.connection_string_interactive_mode(),
                ),
                "ignore_reinit_error": kwargs.get("ignore_reinit_error", True),
                "logging_level": kwargs.get("logging_level", "warning"),
                "resources": kwargs.get("resources", self.resources),
            },
        }

        return ray.init(**init_args)

    def connection_string_interactive_mode(self) -> Optional[str]:
        """Return connection string to compute_resource."""
        if self.host is not None:
            return f"ray://{self.host}:{self.port_interactive}"
        return None

    @classmethod
    def from_dict(cls, data: dict):
        """Create compute_resource object form dict."""
        return ComputeResource(
            name=data.get("name"),
            host=data.get("host"),
            port_interactive=data.get("port_interactive"),
            port_job_server=data.get("port_job_server"),
        )

    def __eq__(self, other: object):
        if isinstance(other, ComputeResource):
            return self.name == other.name and self.host == other.host
        return False

    def __repr__(self):
        return f"<ComputeResource: {self.name}>"


class Provider(JsonSerializable):
    """Provider."""

    def __init__(
        self,
        name: str,
        host: Optional[str] = None,
        token: Optional[str] = None,
        compute_resource: Optional[ComputeResource] = None,
        available_compute_resources: Optional[List[ComputeResource]] = None,
    ):
        """Provider for serverless computation.

        Example:
            >>> provider = Provider(
            >>>    name="<NAME>",
            >>>    host="<HOST>",
            >>>    token="<TOKEN>",
            >>>    compute_resource=ComputeResource(
            >>>        name="<COMPUTE_RESOURCE_NAME>",
            >>>        host="<COMPUTE_RESOURCE_HOST>"
            >>>    ),
            >>> )

        Args:
            name: name of provider
            host: host of provider a.k.a managers host
            token: authentication token for manager
            compute_resource: selected compute_resource from provider
            available_compute_resources: available clusters in provider
        """
        self.name = name
        self.host = host
        self.token = token
        self.compute_resource = compute_resource
        if available_compute_resources is None:
            if compute_resource is not None:
                available_compute_resources = [compute_resource]
            else:
                available_compute_resources = []
        self.available_compute_resources = available_compute_resources

    @classmethod
    def from_dict(cls, dictionary: dict):
        return Provider(**dictionary)

    def job_client(self):
        """Return job client for configured compute resource of provider.

        Returns:
            job client
        """
        return self.compute_resource.job_client()

    def context(self, **kwargs):
        """Allocated context for selected compute_resource for provider."""
        if self.compute_resource is None:
            raise QuantumServerlessException(
                f"ComputeResource was not selected for provider {self.name}"
            )
        return self.compute_resource.context(**kwargs)

    def __eq__(self, other):
        if isinstance(other, Provider):
            return self.name == other.name

        return False

    def __repr__(self):
        return f"<Provider: {self.name}>"

    def get_compute_resources(self) -> List[ComputeResource]:
        """Return compute resources for provider."""
        raise NotImplementedError

    def create_compute_resource(self, resource) -> int:
        """Create compute resource for provider."""
        raise NotImplementedError

    def delete_compute_resource(self, resource) -> int:
        """Delete compute resource for provider."""
        raise NotImplementedError

    def run_program(self, program: Program) -> Job:
        """Execute program as a async job.

        Example:
            >>> serverless = QuantumServerless()
            >>> nested_program = Program(
            >>>     "job.py",
            >>>     arguments={"arg1": "val1"},
            >>>     dependencies=["requests"]
            >>> )
            >>> job = serverless.run_program(nested_program)
            >>> # <Job | ...>

        Args:
            program: program object

        Returns:
            Job
        """
        job_client = self.job_client()

        if job_client is None:
            logging.warning(  # pylint: disable=logging-fstring-interpolation
                f"Job has not been submitted as no provider "
                f"with remote host has been configured. "
                f"Selected provider: {self}"
            )
            return None

        arguments = ""
        if program.arguments is not None:
            arg_list = [f"--{key}={value}" for key, value in program.arguments.items()]
            arguments = " ".join(arg_list)
        entrypoint = f"python {program.entrypoint} {arguments}"

        # set program name so OT can use it as parent span name
        env_vars = {**(program.env_vars or {}), **{OT_PROGRAM_NAME: program.name}}

        job_id = job_client.submit_job(
            entrypoint=entrypoint,
            submission_id=f"qs_{uuid4()}",
            runtime_env={
                "working_dir": program.working_dir,
                "pip": program.dependencies,
                "env_vars": env_vars,
            },
        )
        return Job(job_id=job_id, job_client=job_client)
