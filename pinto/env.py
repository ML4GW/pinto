import os
import re
import shutil
import subprocess
import sys
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

import toml
from cleo.application import Application

from pinto.logging import logger
from pinto.utils import temp_env_set

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from poetry.factory import Factory
    from poetry.installation.installer import Installer
    from poetry.masonry.builders import EditableBuilder
    from poetry.utils.env import EnvManager

try:
    from conda.cli import python_api as conda
    from conda.core.prefix_data import PrefixData
except ImportError:
    raise ImportError("Can only use pinto with conda installed")

if TYPE_CHECKING:
    from .project import Project


@dataclass
class Environment:
    project: "Project"

    def __new__(cls, project: "Project") -> "Environment":
        try:
            with open(project.path / "poetry.toml", "r") as f:
                poetry_config = toml.load(f)
        except FileNotFoundError:
            env_class = PoetryEnvironment
        else:
            try:
                if poetry_config["virtualenvs"]["create"]:
                    env_class = PoetryEnvironment
                else:
                    env_class = CondaEnvironment
            except KeyError:
                env_class = PoetryEnvironment

        obj = object.__new__(env_class)
        return obj

    @property
    def path(self):
        return self.project.path


def _poetry_conda_context(f):
    @wraps(f)
    def wrapper(obj, *args, **kwargs):
        # set this environment variable in case we're running
        # inside a pinto virtual environment so that poetry
        # doesn't know we're inside a virtualenv besides base
        # and decide to use the system env
        with temp_env_set(action="replace", CONDA_DEFAULT_ENV="base"):
            return f(obj, *args, **kwargs)

    return wrapper


@dataclass
class PoetryEnvironment(Environment):
    def __post_init__(self):
        self._poetry = Factory().create_poetry(self.path)
        self._manager = EnvManager(self._poetry)
        self._io = Application.create_io(self)

        # if the actual virtual environment doesn't
        # exist yet, don't create it. But if it does,
        # call `create` to retrieve it and set the attribute
        if not self.exists():
            self._venv = None
        else:
            self.create()

    @_poetry_conda_context
    def get(self):
        return self._manager.get()

    @property
    def env_root(self):
        if self._venv is None:
            # TODO: this isn't exactly right, but the
            # logic is a lot to reimplement and is the
            # environment doesn't exist yet, this seems
            # like a fair thing to return
            return self.get().path
        return self._venv.path

    @property
    def name(self) -> str:
        if self._venv is not None:
            return self._venv.path.name
        return self._manager.generate_env_name(
            self.project.name, str(self.project.path)
        )

    def exists(self) -> bool:
        return self.get() != self._manager.get_system_env()

    @_poetry_conda_context
    def create(self):
        self._venv = self._manager.create_venv(self._io)
        return self._venv

    def contains(self, project: "Project") -> bool:
        if self._venv is None:
            raise ValueError(f"Virtual environment {self.name} not created")

        name = project.name.replace("-", "_")
        return self._venv.site_packages.find_distribution(name) is not None

    def install(
        self, extras: Optional[Iterable[str]] = None, update: bool = False
    ) -> None:
        installer = Installer(
            self._io,
            self._venv,
            self._poetry.package,
            self._poetry.locker,
            self._poetry.pool,
            self._poetry.config,
        )

        if update:
            installer.update(True)
        installer.use_executor(True)
        if extras is not None:
            installer.extras(extras)

        installer.run()

        builder = EditableBuilder(self._poetry, self._venv, self._io)
        builder.build()

    def run(self, bin: str, *args: str) -> None:
        """
        Recycling some of the code from Poetry's Env.execute
        method, but without calling os.execvpe which replaces
        the process and prevents testing/pipeline execution
        """
        command = self._venv.get_command_from_bin(bin) + list(args)
        with temp_env_set(action="insert", PATH=self.env_root / "bin"):
            env = dict(os.environ)
            exe = subprocess.Popen(
                [command[0]] + command[1:], env=env, shell=False
            )
            exe.communicate()

            if exe.returncode:
                sys.exit(exe.returncode)


def _run_conda_command(*args):
    try:
        stdout, stderr, exit_code = conda.run_command(
            *map(str, args), use_exception_handler=False
        )
    except SystemExit:
        raise RuntimeError("System exit raised!")

    if exit_code:
        sys.exit(exit_code)
    return stdout


_base_pattern = re.compile("(?<=-)base$")


def _is_yaml(fname):
    return Path(fname).suffix in (".yml", ".yaml")


def _normalize_env_name(env_name, project_name):
    return _base_pattern.sub(project_name, env_name)


def _env_exists(env_name):
    stdout = _run_conda_command(conda.Commands.INFO, "--envs")
    rows = [i for i in stdout.splitlines() if i and not i.startswith("#")]
    env_names = [i.split()[0] for i in rows]
    return env_name in env_names


def _read_env_name(env_file):
    match = re.search("(?m)(?<=^name: ).+", env_file.read_text())
    if match is None:
        raise ValueError(f"Environment file {env_file} has no 'name' field.")
    return match.group(0)


@dataclass
class CondaEnvironment(Environment):
    @property
    def env_root(self):
        return os.path.join(os.environ["CONDA_ROOT"], "envs", self.name)

    def __post_init__(self):
        try:
            # first check to see if the project pyproject.toml
            # indicates a conda environment to poetry install
            # dependencies on top of
            base_env = self.project.pinto_config["base_env"]
        except KeyError:
            base_env, env_name = self._look_for_environment_file()
        else:
            if _is_yaml(base_env):
                # see if the specified env is actually an environment yaml
                env_name = _read_env_name(base_env)
            else:
                # otherwise assume it's specifying an environment by name
                env_name = base_env
            env_name = _normalize_env_name(env_name, self.project.name)

        self.base_env, self.name = base_env, env_name

    def _look_for_environment_file(self):
        # if conda environment is not specified, begin
        # looking for an `environment.yaml` in every directory
        # going up to the root level from the project directory,
        # taking the first environment.yaml we can find
        env_dir = self.path / "*"
        while env_dir.parent != env_dir:
            env_dir = env_dir.parent

            # try both yaml suffixes for generality
            for suffix in ["yaml", "yml"]:
                base_env = env_dir / f"environment.{suffix}"
                if base_env.exists():
                    break
            else:
                # the for-loop never broke, so neither
                # suffix exists at this level, move on
                # up to the next one
                continue
            break
        else:
            # if we've hit the root level, we don't know
            # what environment to use so raise an error
            raise ValueError(
                "No environment file in directory tree "
                "of project {}".format(self.project.path)
            )

        env_name = _read_env_name(base_env)
        if env_dir != self.path:
            # if this environment file doesn't live
            # in the project's directory, then take
            # it as the intended environment name
            if _base_pattern.search(env_name) is not None:
                env_name = _normalize_env_name(env_name, self.project.name)
            else:
                env_name = self.project.name
        return base_env, env_name

    def exists(self):
        return _env_exists(self.name)

    def create(self):
        # TODO: do this check in the parent class?
        if self.exists():
            logger.warning(f"Environment {self.name} already exists")
            return

        # if the base environment is specified as a yaml
        # file, check the name to see if it exists
        if _is_yaml(self.base_env):
            env_name = _read_env_name(self.base_env)

            # if the environment doesn't exist yet, create it
            # using the indicated environnment file
            if not _env_exists(env_name):
                logger.info(
                    "Creating conda environment {} "
                    "from environment file {}".format(env_name, self.base_env)
                )

                # unfortunately the conda python api doesn't support
                # creating from an environment file, so call this
                # subprocess manually
                conda_cmd = f"conda env create -f {self.base_env}"
                response = subprocess.run(
                    conda_cmd, shell=True, capture_output=True, text=True
                )
                if response.returncode:
                    raise RuntimeError(
                        "Conda command '{}'' failed with return code {} "
                        "and stderr:\n{}".format(
                            conda_cmd, response.returncode, response.stderr
                        )
                    )
                logger.info(response.stdout)

            # if the specified environment file is for
            # _this_ environment, then we're done here
            if env_name == self.name:
                return
        else:
            env_name = self.base_env

        if not _env_exists(env_name):
            raise ValueError(f"No base Conda environment {env_name} to clone")

        # otherwise create a fresh environment
        # by cloning the indicated environment
        logger.info(
            "Creating environment {} by cloning from environment {}".format(
                self.name, env_name
            )
        )
        _run_conda_command(
            conda.Commands.CREATE, "-n", self.name, "--clone", env_name
        )

    def contains(self, project: "Project") -> bool:
        project_name = project.name.replace("_", "-")
        regex = re.compile(f"(?m)^{project_name} ")
        package_list = _run_conda_command(conda.Commands.LIST, "-n", self.name)
        return regex.search(package_list) is not None

    def install(
        self, extras: Optional[Iterable[str]] = None, update: bool = False
    ) -> None:
        # use poetry binary explicitly since activating
        # environment may remove location from $PATH
        poetry_bin = shutil.which("poetry")
        poetry_cmd = "update" if update else "install"
        cmd = f"cd {self.project.path} && {poetry_bin} {poetry_cmd}"

        # specify any extras and execute command
        if extras is not None:
            for extra in extras:
                cmd += f" -E {extra}"
        self.run("/bin/bash", "-c", cmd)

        # Conda caches calls to `conda list`, so manually update
        # the cache to reflect the newly pip-installed packages
        try:
            PrefixData._cache_.pop(self.env_root)
        except KeyError:
            pass

    @contextmanager
    def _insert_base_ld_lib(self):
        """
        sometimes conda environments will point to c++ libs
        installed in the base environment. You can get around
        conflicts by manually inserting the base environment
        lib directory at the front of LD_LIBRARY_PATH. Check
        if the user's pinto config indicates that they'd like
        to do this, and that there's a valid base conda prefix
        to use to insert in the first place
        """

        prefix = os.getenv("CONDA_PREFIX")
        conda_config = self.project.pinto_config.get("conda", {})
        insert_ld_lib = conda_config.get("append_base_ld_library_path", False)
        insert_ld_lib &= prefix is not None

        if not insert_ld_lib:
            yield
        else:
            ld_lib_path = f"{prefix}/envs/{self.name}/lib:{prefix}/lib"
            with temp_env_set(action="append", LD_LIBRARY_PATH=ld_lib_path):
                yield

    def run(self, bin: str, *args: str) -> None:
        with self._insert_base_ld_lib():
            _run_conda_command(
                conda.Commands.RUN,
                "-n",
                self.name,
                "--no-capture-output",
                bin,
                *args,
            )
