import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from multiprocessing import cpu_count
from typing import Any, ClassVar, Dict, FrozenSet, Optional
from urllib.parse import urlparse

from funcy import cached_property, decorator

from dvc.dir_info import DirInfo
from dvc.exceptions import DvcException, DvcIgnoreInCollectedDirError
from dvc.hash_info import HashInfo
from dvc.ignore import DvcIgnore
from dvc.path_info import URLInfo
from dvc.progress import Tqdm
from dvc.state import StateNoop
from dvc.utils import tmp_fname
from dvc.utils.fs import makedirs, move
from dvc.utils.http import open_url

logger = logging.getLogger(__name__)


class RemoteCmdError(DvcException):
    def __init__(self, remote, cmd, ret, err):
        super().__init__(
            "{remote} command '{cmd}' finished with non-zero return code"
            " {ret}': {err}".format(remote=remote, cmd=cmd, ret=ret, err=err)
        )


class RemoteActionNotImplemented(DvcException):
    def __init__(self, action, scheme):
        m = f"{action} is not supported for {scheme} remotes"
        super().__init__(m)


class RemoteMissingDepsError(DvcException):
    pass


@decorator
def use_state(call):
    tree = call._args[0]  # pylint: disable=protected-access
    with tree.state:
        return call()


class BaseTree:
    scheme = "base"
    REQUIRES: ClassVar[Dict[str, str]] = {}
    PATH_CLS = URLInfo  # type: Any
    JOBS = 4 * cpu_count()

    CHECKSUM_DIR_SUFFIX = ".dir"
    HASH_JOBS = max(1, min(4, cpu_count() // 2))
    LIST_OBJECT_PAGE_SIZE = 1000
    TRAVERSE_WEIGHT_MULTIPLIER = 5
    TRAVERSE_PREFIX_LEN = 3
    TRAVERSE_THRESHOLD_SIZE = 500000
    CAN_TRAVERSE = True

    # Needed for some providers, and http open()
    CHUNK_SIZE = 64 * 1024 * 1024  # 64 MiB

    PARAM_CHECKSUM: ClassVar[Optional[str]] = None
    DETAIL_FIELDS: FrozenSet[str] = frozenset()

    state = StateNoop()

    def __init__(self, repo, config):
        self.repo = repo
        self.config = config

        self._check_requires()

        self.path_info = None

    @cached_property
    def jobs(self):
        return (
            self.config.get("jobs")
            or (self.repo and self.repo.config["core"].get("jobs"))
            or self.JOBS
        )

    @cached_property
    def hash_jobs(self):
        return (
            self.config.get("checksum_jobs")
            or (self.repo and self.repo.config["core"].get("checksum_jobs"))
            or self.HASH_JOBS
        )

    @classmethod
    def get_missing_deps(cls):
        import importlib

        missing = []
        for package, module in cls.REQUIRES.items():
            try:
                importlib.import_module(module)
            except ImportError:
                missing.append(package)

        return missing

    def _check_requires(self):
        from ..scheme import Schemes
        from ..utils import format_link
        from ..utils.pkg import PKG

        missing = self.get_missing_deps()
        if not missing:
            return

        url = self.config.get("url", f"{self.scheme}://")

        scheme = self.scheme
        if scheme == Schemes.WEBDAVS:
            scheme = Schemes.WEBDAV

        by_pkg = {
            "pip": f"pip install 'dvc[{scheme}]'",
            "conda": f"conda install -c conda-forge dvc-{scheme}",
        }

        cmd = by_pkg.get(PKG)
        if cmd:
            link = format_link("https://dvc.org/doc/install")
            hint = (
                f"To install dvc with those dependencies, run:\n"
                "\n"
                f"\t{cmd}\n"
                "\n"
                f"See {link} for more info."
            )
        else:
            link = format_link("https://github.com/iterative/dvc/issues")
            hint = f"Please report this bug to {link}. Thank you!"

        raise RemoteMissingDepsError(
            f"URL '{url}' is supported but requires these missing "
            f"dependencies: {missing}. {hint}"
        )

    @classmethod
    def supported(cls, config):
        if isinstance(config, (str, bytes)):
            url = config
        else:
            url = config["url"]

        # NOTE: silently skipping remote, calling code should handle that
        parsed = urlparse(url)
        return parsed.scheme == cls.scheme

    @property
    def cache(self):
        return getattr(self.repo.cache, self.scheme)

    def open(self, path_info, mode: str = "r", encoding: str = None, **kwargs):
        if hasattr(self, "_generate_download_url"):
            # pylint:disable=no-member
            func = self._generate_download_url  # type: ignore[attr-defined]
            get_url = partial(func, path_info)
            return open_url(get_url, mode=mode, encoding=encoding, **kwargs)

        raise RemoteActionNotImplemented("open", self.scheme)

    def exists(self, path_info, use_dvcignore=True) -> bool:
        raise NotImplementedError

    # pylint: disable=unused-argument

    def isdir(self, path_info):
        """Optional: Overwrite only if the remote has a way to distinguish
        between a directory and a file.
        """
        return False

    def isfile(self, path_info):
        """Optional: Overwrite only if the remote has a way to distinguish
        between a directory and a file.
        """
        return True

    def isexec(self, path_info):
        """Optional: Overwrite only if the remote has a way to distinguish
        between executable and non-executable file.
        """
        return False

    def iscopy(self, path_info):
        """Check if this file is an independent copy."""
        return False  # We can't be sure by default

    def walk_files(self, path_info, **kwargs):
        """Return a generator with `PathInfo`s to all the files.

        Optional kwargs:
            prefix (bool): If true `path_info` will be treated as a prefix
                rather than directory path.
        """
        raise NotImplementedError

    def ls(self, path_info, detail=False, **kwargs):
        raise RemoteActionNotImplemented("ls", self.scheme)

    def is_empty(self, path_info):
        return False

    def getsize(self, path_info):
        return None

    def remove(self, path_info):
        raise RemoteActionNotImplemented("remove", self.scheme)

    def makedirs(self, path_info):
        """Optional: Implement only if the remote needs to create
        directories before copying/linking/moving data
        """

    def move(self, from_info, to_info):
        self.copy(from_info, to_info)
        self.remove(from_info)

    def copy(self, from_info, to_info):
        raise RemoteActionNotImplemented("copy", self.scheme)

    def symlink(self, from_info, to_info):
        raise RemoteActionNotImplemented("symlink", self.scheme)

    def hardlink(self, from_info, to_info):
        raise RemoteActionNotImplemented("hardlink", self.scheme)

    def reflink(self, from_info, to_info):
        raise RemoteActionNotImplemented("reflink", self.scheme)

    # pylint: enable=unused-argument

    @classmethod
    def is_dir_hash(cls, hash_):
        if not hash_:
            return False
        return hash_.endswith(cls.CHECKSUM_DIR_SUFFIX)

    @use_state
    def get_hash(self, path_info, **kwargs):
        assert path_info and (
            isinstance(path_info, str) or path_info.scheme == self.scheme
        )

        if not self.exists(path_info):
            return None

        # pylint: disable=assignment-from-none
        hash_info = self.state.get(path_info)

        # If we have dir hash in state db, but dir cache file is lost,
        # then we need to recollect the dir via .get_dir_hash() call below,
        # see https://github.com/iterative/dvc/issues/2219 for context
        if (
            hash_info
            and hash_info.isdir
            and not self.cache.tree.exists(
                self.cache.tree.hash_to_path_info(hash_info.value)
            )
        ):
            hash_info = None

        if hash_info:
            assert hash_info.name == self.PARAM_CHECKSUM
            if hash_info.isdir:
                self.cache.set_dir_info(hash_info)
            return hash_info

        if self.isdir(path_info):
            hash_info = self.get_dir_hash(path_info, **kwargs)
        else:
            hash_info = self.get_file_hash(path_info)

        if hash_info and self.exists(path_info):
            self.state.save(path_info, hash_info)

        return hash_info

    def get_file_hash(self, path_info):
        raise NotImplementedError

    def hash_to_path_info(self, hash_):
        return self.path_info / hash_[0:2] / hash_[2:]

    def _calculate_hashes(self, file_infos):
        with Tqdm(
            total=len(file_infos),
            unit="md5",
            desc="Computing file/dir hashes (only done once)",
        ) as pbar:
            worker = pbar.wrap_fn(self.get_file_hash)
            with ThreadPoolExecutor(max_workers=self.hash_jobs) as executor:
                hash_infos = executor.map(worker, file_infos)
                return dict(zip(file_infos, hash_infos))

    def _iter_hashes(self, path_info, **kwargs):
        if self.PARAM_CHECKSUM in self.DETAIL_FIELDS:
            for details in self.ls(path_info, recursive=True, detail=True):
                file_info = path_info.replace(path=details["name"])
                hash_info = HashInfo(
                    self.PARAM_CHECKSUM,
                    details[self.PARAM_CHECKSUM],
                    size=details.get("size"),
                )
                yield file_info, hash_info

            return None

        file_infos = []
        for file_info in self.walk_files(path_info, **kwargs):
            hash_info = self.state.get(  # pylint: disable=assignment-from-none
                file_info
            )
            if not hash_info:
                file_infos.append(file_info)
                continue
            yield file_info, hash_info

        yield from self._calculate_hashes(file_infos).items()

    def _collect_dir(self, path_info, **kwargs):
        dir_info = DirInfo()
        for fi, hi in self._iter_hashes(path_info, **kwargs):
            if DvcIgnore.DVCIGNORE_FILE == fi.name:
                raise DvcIgnoreInCollectedDirError(fi.parent)

            # NOTE: this is lossy transformation:
            #   "hey\there" -> "hey/there"
            #   "hey/there" -> "hey/there"
            # The latter is fine filename on Windows, which
            # will transform to dir/file on back transform.
            #
            # Yes, this is a BUG, as long as we permit "/" in
            # filenames on Windows and "\" on Unix
            dir_info.trie[fi.relative_to(path_info).parts] = hi

        return dir_info

    @use_state
    def get_dir_hash(self, path_info, **kwargs):
        dir_info = self._collect_dir(path_info, **kwargs)
        hash_info = self.repo.cache.local.save_dir_info(dir_info)
        hash_info.size = dir_info.size
        return hash_info

    def upload(
        self, from_info, to_info, name=None, no_progress_bar=False,
    ):
        if not hasattr(self, "_upload"):
            raise RemoteActionNotImplemented("upload", self.scheme)

        if to_info.scheme != self.scheme:
            raise NotImplementedError

        if from_info.scheme != "local":
            raise NotImplementedError

        logger.debug("Uploading '%s' to '%s'", from_info, to_info)

        name = name or from_info.name

        self._upload(  # noqa, pylint: disable=no-member
            from_info.fspath,
            to_info,
            name=name,
            no_progress_bar=no_progress_bar,
        )

    def upload_fobj(self, fobj, to_info, no_progress_bar=False, **pbar_args):
        if not hasattr(self, "_upload_fobj"):
            raise RemoteActionNotImplemented("upload_fobj", self.scheme)

        with Tqdm.wrapattr(
            fobj, "read", disable=no_progress_bar, bytes=True, **pbar_args
        ) as wrapped:
            self._upload_fobj(wrapped, to_info)  # pylint: disable=no-member

    def download(
        self,
        from_info,
        to_info,
        name=None,
        no_progress_bar=False,
        jobs=None,
        **kwargs,
    ):
        if not hasattr(self, "_download"):
            raise RemoteActionNotImplemented("download", self.scheme)

        if from_info.scheme != self.scheme:
            raise NotImplementedError

        if to_info.scheme == self.scheme != "local":
            self.copy(from_info, to_info)
            return 0

        if to_info.scheme != "local":
            raise NotImplementedError

        if self.isdir(from_info):
            return self._download_dir(
                from_info, to_info, name, no_progress_bar, jobs, **kwargs,
            )
        return self._download_file(from_info, to_info, name, no_progress_bar,)

    def _download_dir(
        self, from_info, to_info, name, no_progress_bar, jobs, **kwargs,
    ):
        from_infos = list(self.walk_files(from_info, **kwargs))
        to_infos = (
            to_info / info.relative_to(from_info) for info in from_infos
        )

        with Tqdm(
            total=len(from_infos),
            desc="Downloading directory",
            unit="Files",
            disable=no_progress_bar,
        ) as pbar:
            download_files = pbar.wrap_fn(
                partial(self._download_file, name=name, no_progress_bar=True,)
            )
            max_workers = jobs or self.jobs
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(download_files, from_info, to_info)
                    for from_info, to_info in zip(from_infos, to_infos)
                ]

                # NOTE: unlike pulling/fetching cache, where we need to
                # download everything we can, not raising an error here might
                # turn very ugly, as the user might think that he has
                # downloaded a complete directory, while having a partial one,
                # which might cause unexpected results in his pipeline.
                for future in as_completed(futures):
                    # NOTE: executor won't let us raise until all futures that
                    # it has are finished, so we need to cancel them ourselves
                    # before re-raising.
                    exc = future.exception()
                    if exc:
                        for entry in futures:
                            entry.cancel()
                        raise exc

    def _download_file(
        self, from_info, to_info, name, no_progress_bar,
    ):
        makedirs(to_info.parent, exist_ok=True)

        logger.debug("Downloading '%s' to '%s'", from_info, to_info)
        name = name or to_info.name

        tmp_file = tmp_fname(to_info)

        self._download(  # noqa, pylint: disable=no-member
            from_info, tmp_file, name=name, no_progress_bar=no_progress_bar
        )

        move(tmp_file, to_info)
