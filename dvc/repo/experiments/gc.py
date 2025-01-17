import logging
from typing import Optional

from dvc.repo import locked

from .base import EXEC_APPLY, EXEC_BRANCH, EXEC_CHECKPOINT
from .utils import exp_refs

logger = logging.getLogger(__name__)


@locked
def gc(
    repo,
    all_branches: Optional[bool] = False,
    all_tags: Optional[bool] = False,
    all_commits: Optional[bool] = False,
    workspace: Optional[bool] = False,
    queued: Optional[bool] = False,
):
    keep_revs = set(
        repo.brancher(
            all_branches=all_branches,
            all_tags=all_tags,
            all_commits=all_commits,
            sha_only=True,
        )
    )
    if workspace:
        keep_revs.add(repo.scm.get_rev())

    if not keep_revs:
        return 0

    exec_branch = repo.scm.get_ref(EXEC_BRANCH, follow=False)
    exec_apply = repo.scm.get_ref(EXEC_APPLY)
    exec_checkpoint = repo.scm.get_ref(EXEC_CHECKPOINT)

    removed = 0
    for ref_info in exp_refs(repo.scm):
        if ref_info.baseline_sha not in keep_revs:
            ref = repo.scm.get_ref(str(ref_info))
            if exec_branch and str(ref_info):
                repo.scm.remove_ref(EXEC_BRANCH)
            if exec_apply and exec_apply == ref:
                repo.scm.remove_ref(EXEC_APPLY)
            if exec_checkpoint and exec_checkpoint == ref:
                repo.scm.remove_ref(EXEC_CHECKPOINT)
            repo.scm.remove_ref(str(ref_info))
            removed += 1

    delete_stashes = []
    for _, entry in repo.experiments.stash_revs.items():
        if not queued or entry.baseline_rev not in keep_revs:
            delete_stashes.append(entry.index)
    for index in sorted(delete_stashes, reverse=True):
        repo.experiments.stash.drop(index)
    removed += len(delete_stashes)

    return removed
