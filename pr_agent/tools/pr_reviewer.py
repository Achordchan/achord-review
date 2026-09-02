import asyncio
import copy
import datetime
import re
from functools import partial
from typing import List, Optional, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.inline_comment_dedup import (
    InlineCommentStore,
    can_verify_inline_comment_publication,
    get_inline_comment_store,
    key_issue_anchor_fingerprint,
    key_issue_body_with_markers,
    key_issue_fingerprint,
    key_issue_location_fingerprint,
)
from pr_agent.algo.pr_processing import add_ai_metadata_to_diff_files, get_pr_diff, retry_with_fallback_models
from pr_agent.algo.publish_lock import publish_lock
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.review_parser import recover_missing_review_wrapper
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (
    ModelType,
    PRReviewHeader,
    PRReviewIdentity,
    add_pr_review_identity,
    clean_review_message,
    convert_to_markdown_v2,
    format_severity_badge,
    github_action_output,
    load_yaml,
    show_relevant_configurations,
    show_run_details,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import IncrementalPR, get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.ticket_pr_compliance_check import extract_and_cache_pr_tickets

MAX_REVIEW_COVERAGE_FILES = 50
_SUGGESTION_FENCE_RE = re.compile(r"```[ \t]*suggestion\b", re.IGNORECASE)


VERDICT_REASON_PREFIX = "**Verdict:** "
VERDICT_EVENT_TO_STATE = {"APPROVE": "APPROVED",
                          "REQUEST_CHANGES": "CHANGES_REQUESTED",
                          "COMMENT": "COMMENTED"}
# "the standing verdict was never read", as distinct from "no verdict was standing".
# Only the former disables the concurrent-run guard in _publish_single_review.
_VERDICT_SNAPSHOT_UNSET = object()


def _verdict_is_newer(standing, snapshot) -> bool:
    """True when `standing` was published after `snapshot` was taken.

    Both are (commit, review id). GitHub review ids increase monotonically, so "newer" is
    decidable; plain inequality is not enough, because dismissing the newest review makes
    an older one stand again, and reading that as a concurrent publication would silence a
    review nobody else had answered. Providers that expose no id fall back to the commit.
    """
    standing_sha, standing_id = standing
    snapshot_sha, snapshot_id = snapshot
    if isinstance(standing_id, int) and isinstance(snapshot_id, int):
        return standing_id > snapshot_id
    return standing_sha != snapshot_sha


class PRReviewer:
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """

    def __init__(self, pr_url: str, is_answer: bool = False, is_auto: bool = False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        """
        Initialize the PRReviewer object with the necessary attributes and objects to review a pull request.

        Args:
            pr_url (str): The URL of the pull request to be reviewed.
            is_answer (bool, optional): Indicates whether the review is being done in answer mode. Defaults to False.
            is_auto (bool, optional): Indicates whether the review is being done in automatic mode. Defaults to False.
            ai_handler (BaseAiHandler): The AI handler to be used for the review. Defaults to None.
            args (list, optional): List of arguments passed to the PRReviewer class. Defaults to None.
        """
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args
        self.incremental = self.parse_incremental(args)  # -i command
        if self.incremental and self.incremental.is_incremental:
            self.git_provider.get_incremental_commits(self.incremental)

        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_url = pr_url
        self.is_answer = is_answer
        self.is_auto = is_auto

        if self.is_answer and not self.git_provider.is_supported("get_issue_comments"):
            raise Exception(f"Answer mode is not supported for {get_settings().config.git_provider} for now")
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.remaining_files_list = []
        self.prediction = None
        self.review_data = None
        self.deferred_review_comments = []
        # Findings the model raised again that were already posted on an earlier review and
        # deduped here, so they produce no new inline comment. Counted so a re-review with
        # nothing new to show can say so rather than emit a verdict pointing at nothing.
        self.carried_over_findings = 0
        question_str, answer_str = self._get_user_answers()
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug(f"AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug(f"AI metadata is disabled for this command")

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "num_pr_files": self.git_provider.get_num_of_files(),
            "num_max_findings": get_settings().pr_reviewer.num_max_findings,
            "require_score": get_settings().pr_reviewer.require_score_review,
            "require_tests": get_settings().pr_reviewer.require_tests_review,
            "require_estimate_effort_to_review": get_settings().pr_reviewer.require_estimate_effort_to_review,
            "require_estimate_contribution_time_cost": get_settings().pr_reviewer.require_estimate_contribution_time_cost,
            'require_can_be_split_review': get_settings().pr_reviewer.require_can_be_split_review,
            'require_security_review': get_settings().pr_reviewer.require_security_review,
            'require_todo_scan': get_settings().pr_reviewer.get("require_todo_scan", False),
            'require_severity': get_settings().pr_reviewer.get("enable_review_verdict", False),
            'question_str': question_str,
            'answer_str': answer_str,
            "extra_instructions": get_settings().pr_reviewer.extra_instructions,
            "skills_context": get_skills_context(),
            "repo_context": build_repo_context(self.git_provider),
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "custom_labels": "",
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "is_ai_metadata":  get_settings().get("config.enable_ai_metadata", False),
            "related_tickets": get_settings().get('related_tickets', []),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
            "date": datetime.datetime.now().strftime('%Y-%m-%d'),
        }

        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            get_settings().pr_review_prompt.system,
            get_settings().pr_review_prompt.user
        )

    def parse_incremental(self, args: List[str]):
        is_incremental = False
        if args and len(args) >= 1:
            arg = args[0]
            if arg == "-i":
                is_incremental = True
        incremental = IncrementalPR(is_incremental)
        return incremental

    async def run(self) -> None:
        init_run_details()
        progress_response = None
        review_failed = False
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping review")
                return None

            if self.incremental.is_incremental:
                can_run = self._can_run_incremental_review()
                # If the gate disabled incremental (e.g., commits_range is None), fall through to full review.
                if not can_run and self.incremental.is_incremental:
                    return None

            # if isinstance(self.args, list) and self.args and self.args[0] == 'auto_approve':
            #     get_logger().info(f'Auto approve flow PR: {self.pr_url} ...')
            #     self.auto_approve_logic()
            #     return None

            get_logger().info(f'Reviewing PR: {self.pr_url} ...')
            relevant_configs = {'pr_reviewer': dict(get_settings().pr_reviewer),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # ticket extraction if exists
            await extract_and_cache_pr_tickets(self.git_provider, self.vars)

            if (
                self.incremental.is_incremental
                and hasattr(self.git_provider, "unreviewed_files_map")
                and not self.git_provider.unreviewed_files_map
            ):
                get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new files")
                previous_review_url = ""
                if hasattr(self.git_provider, "previous_review") and self.git_provider.previous_review is not None:
                    previous_review_url = getattr(self.git_provider.previous_review, "html_url", "") or ""
                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(f"Incremental Review Skipped\n"
                                    f"No files were changed since the [previous PR Review]({previous_review_url})")
                return None

            if get_settings().config.publish_output and not get_settings().config.get('is_auto_command', False):
                progress_response = self.git_provider.publish_comment("Preparing review...", is_temporary=True)

            # Read before the model call, not after: the guard in _publish_single_review needs
            # to tell a verdict that was already standing from one a concurrent run posted
            # while this one was thinking.
            verdict_at_start = _VERDICT_SNAPSHOT_UNSET
            if self._single_review_submission_enabled() and get_settings().config.publish_output:
                verdict_at_start = self._standing_verdict()

            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            if not self.prediction:
                return None

            pr_review = self._prepare_pr_review()
            get_logger().debug(f"PR output", artifact=pr_review)

            if self._single_review_submission_enabled() and get_settings().config.publish_output:
                # Off the event loop: the publish path waits on a cross-process lock, and a
                # worker that blocks there stops answering webhooks. In a thread the wait
                # can be long enough to be worth having.
                await asyncio.to_thread(self._publish_single_review, pr_review, verdict_at_start)
                return

            should_publish = get_settings().config.publish_output and self._should_publish_review_no_suggestions(pr_review)
            if not should_publish:
                reason = "Review output is not published"
                if get_settings().config.publish_output:
                    reason += ": no major issues detected."
                get_logger().info(reason)
                get_settings().data = {"artifact": pr_review}
                self._submit_review_verdict()
                return

            # publish the review
            # Providers that support it (GitLab) can post the review's final comment as a resolvable thread.
            # This intent applies to the review only - never to status comments or the output of other tools.
            review_thread_kwargs = {"as_thread": True} if self.git_provider.should_publish_review_as_thread() else {}
            if get_settings().pr_reviewer.persistent_comment and not self.incremental.is_incremental:
                final_update_message = get_settings().pr_reviewer.final_update_message
                self.git_provider.publish_persistent_comment(
                    pr_review,
                    initial_header=pr_review.split("\n", 1)[0],
                    update_header=True,
                    final_update_message=final_update_message,
                    identity_marker=PRReviewIdentity.REGULAR.value,
                    legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                    **review_thread_kwargs,
                )
            else:
                if self.git_provider.supports_review_comment_identity() is True:
                    identity_marker = (
                        PRReviewIdentity.INCREMENTAL.value
                        if self.incremental.is_incremental
                        else PRReviewIdentity.REGULAR.value
                    )
                    pr_review = add_pr_review_identity(pr_review, identity_marker)
                self.git_provider.publish_comment(pr_review, **review_thread_kwargs)

            self._submit_review_verdict()
        except Exception as e:
            review_failed = True
            get_logger().error(f"Failed to review PR: {e}")
            if get_settings().config.get("propagate_tool_errors", False):
                raise
        finally:
            if progress_response is not None:
                try:
                    self.git_provider.remove_comment(progress_response)
                except Exception as e:
                    get_logger().exception(f"Failed to remove review progress comment, error: {e}")
            if (review_failed and get_settings().config.publish_output and
                    not get_settings().config.get("is_auto_command", False)):
                try:
                    self.git_provider.publish_comment("Failed to review PR")
                except Exception as e:
                    get_logger().exception(f"Failed to publish review failure result, error: {e}")

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        return get_settings().pr_reviewer.get('publish_output_no_suggestions', True) or "No major issues detected" not in pr_review

    def _single_review_submission_enabled(self) -> bool:
        """One review carrying summary, findings and verdict, instead of three notifications."""
        return bool(get_settings().pr_reviewer.get('single_review_submission', False)
                    and get_settings().pr_reviewer.get('enable_review_verdict', False)
                    and callable(getattr(self.git_provider, "submit_review_verdict", None)))

    def _standing_verdict(self):
        """Identity of the verdict standing when this run began, as (commit, review id).

        The commit alone is not enough to recognise a concurrent run: when the head
        already carried a verdict, two concurrent re-reviews of it snapshot the same sha,
        and the second could not tell the first's new review from the one that was
        already there. The review id distinguishes them.

        Returns _VERDICT_SNAPSHOT_UNSET when the standing verdict cannot be read, which
        disables the concurrent-run guard rather than risking a silenced review.
        """
        try:
            standing = self.git_provider.get_latest_own_verdict()
        except Exception as e:
            get_logger().warning(f"Failed to read the standing review verdict, error: {e}")
            return _VERDICT_SNAPSHOT_UNSET
        if not getattr(standing, "read_ok", True):
            # A provider that swallows its own read error hands back an empty verdict, which
            # would snapshot as "nothing was standing" and make any verdict found later look
            # like a concurrent run's - silencing a review someone asked for.
            get_logger().warning("The standing review verdict could not be read; "
                                 "the concurrent-run guard is off for this run")
            return _VERDICT_SNAPSHOT_UNSET
        return standing.sha, standing.review_id

    def _is_manual_invocation(self) -> bool:
        """True when a person asked for this review, rather than a webhook replaying it.

        The repeat guards below exist to stop automatic triggers from re-posting a review
        nobody asked for. Someone typing the command is not that: they are owed an answer,
        and staying silent there reads as the bot being broken rather than as restraint.
        """
        return not get_settings().config.get("is_auto_command", False)

    def _determine_review_verdict(self, data: dict) -> Tuple[str, str]:
        """Map a parsed review into a formal verdict, returning (event, reason)."""
        review = data.get('review') or {}

        if get_settings().pr_reviewer.get('verdict_blocking_on_security_concerns', True):
            security_concerns = str(review.get('security_concerns') or '').strip()
            # the prompt asks for a bare "No" when there is nothing to report
            if security_concerns and not security_concerns.lower().startswith('no'):
                return "REQUEST_CHANGES", "security concerns were raised"

        blocking_severities = {str(severity).strip().upper() for severity in
                               get_settings().pr_reviewer.get('verdict_blocking_severities', ["P0", "P1"])}
        issues = review.get('key_issues_to_review')
        issues = issues if isinstance(issues, list) else []
        found = {str(issue.get('severity') or '').strip().upper() for issue in issues if isinstance(issue, dict)}
        blocking_found = sorted(found & blocking_severities)
        if blocking_found:
            return "REQUEST_CHANGES", f"blocking issues found ({', '.join(blocking_found)})"
        if issues:
            return "COMMENT", "non-blocking suggestions only"
        return "APPROVE", "no blocking issues found"

    def _publish_single_review(self, pr_review: str, verdict_at_start=_VERDICT_SNAPSHOT_UNSET) -> None:
        """Post the summary, the inline findings and the verdict as one review.

        Silence is the right outcome for an automatic re-review that found nothing new:
        with no new findings and an unchanged verdict there is nothing to say, and saying
        it anyway is what turns every push into another notification. A review someone
        asked for is never silenced - see _is_manual_invocation.

        'verdict_at_start' is the verdict standing when this run began, from
        _standing_verdict, and is what distinguishes a concurrent run from a repeat
        request.
        """
        if not self.review_data:
            # _prepare_pr_review returns "" and leaves review_data unset when the model
            # output fails to parse. _determine_review_verdict reads empty data as "no
            # blocking issues" and would sign this off as a clean APPROVE - a broken review
            # dressed up as a pass, the one outcome the gate must never produce. Fail loudly
            # instead, the same stance _submit_review_verdict takes for the older path.
            raise ValueError("No parsed review data available; refusing to publish a review verdict")
        comments = self.deferred_review_comments
        try:
            event, reason = self._determine_review_verdict(self.review_data or {})
        except Exception as e:
            get_logger().exception(f"Failed to determine review verdict, falling back to COMMENT, error: {e}")
            event, reason = "COMMENT", "the review verdict could not be determined"

        # Reading the standing verdict and then publishing is two operations, and the guard
        # below is only as good as the gap between them: two runs that finish together would
        # both read the old verdict and both publish. Holding the lock across the whole
        # section makes the check and the publication one step for a given PR.
        with publish_lock(self.pr_url):
            self._publish_single_review_locked(pr_review, verdict_at_start, comments, event, reason)

    def _publish_single_review_locked(self, pr_review: str, verdict_at_start, comments, event, reason) -> None:
        """The check-and-publish section of _publish_single_review, run under its lock."""
        standing = self.git_provider.get_latest_own_verdict()
        previous_state, reviewed_sha = standing.state, standing.sha
        head_sha = self.git_provider.get_head_commit_sha()
        # A push and a mention of the bot in the comment that follows it are two triggers
        # seconds apart on the same head commit, so both runs read the standing verdict
        # before either posts and neither can see the other by state alone. A verdict for
        # our own head commit that is not the one standing when we started came from that
        # other run: it is the answer, and repeating it is the duplicate review. This
        # outranks the explicit-request exemption below - the requester has been answered.
        if (head_sha and reviewed_sha == head_sha
                and verdict_at_start is not _VERDICT_SNAPSHOT_UNSET
                and _verdict_is_newer((reviewed_sha, standing.review_id), verdict_at_start)):
            get_logger().info(f"Commit {head_sha[:8]} was reviewed by a concurrent run while this one was "
                              f"thinking; staying silent instead of posting the same review twice")
            return
        if self._is_manual_invocation():
            get_logger().info("Review was requested explicitly; answering even if this commit was already reviewed")
        else:
            # The model rewords a finding and shifts its line range between runs, so content
            # matching cannot recognise a repeat. The reviewed commit can: the same code
            # reviewed twice has nothing new to say, whatever the wording.
            if reviewed_sha and head_sha and reviewed_sha == head_sha:
                get_logger().info(f"Commit {head_sha[:8]} was already reviewed ({previous_state}); staying silent")
                return
            if not comments and previous_state == VERDICT_EVENT_TO_STATE.get(event):
                get_logger().info(f"Nothing new to report and the verdict is unchanged ({event}); staying silent")
                return

        # A review that found nothing should say so in words, not report an empty verdict:
        # "no blocking issues found" is the same news, read as a shrug.
        carried_over = getattr(self, "carried_over_findings", 0)
        if event == "APPROVE" and not comments:
            closing = f"\u2705 {clean_review_message(head_sha or '')}"
        elif not comments and carried_over:
            # A non-APPROVE verdict with no comments to show reads as broken: the findings
            # behind it were all raised on an earlier review and deduped here, so there is
            # nothing new to attach. Say that plainly instead of a bare verdict pointing at
            # comments that are not on this review - they still stand as inline comments
            # already on the PR.
            sha_ref = f" `{head_sha[:8]}`" if head_sha else ""
            point = "point" if carried_over == 1 else "points"
            stands = "still stands" if carried_over == 1 else "still stand"
            closing = (f"\U0001F501 Re-reviewed{sha_ref} - no new findings on this commit. "
                       f"{carried_over} {point} from the earlier review {stands}; "
                       f"see the inline comments already on this PR.\n\n"
                       f"{VERDICT_REASON_PREFIX}{reason}.")
        else:
            closing = f"{VERDICT_REASON_PREFIX}{reason}."
        # The marker must ride on whichever review carries the verdict, or the next run
        # cannot see the standing verdict and repeats itself.
        body = self.git_provider.mark_review_verdict_body(f"{pr_review}\n\n{closing}")
        get_logger().info(f"Submitting one review: {event} ({reason}), {len(comments)} inline finding(s)")
        if comments:
            if self.git_provider.publish_code_suggestions(comments, review_body=body, review_event=event):
                return
            get_logger().warning("Single review submission failed; falling back to a verdict-only review")
        if not self.git_provider.submit_review_verdict(event, body):
            get_logger().info(f"Review verdict {event} was not submitted")

    def _submit_review_verdict(self) -> None:
        if not get_settings().pr_reviewer.get('enable_review_verdict', False):
            return
        if not get_settings().config.publish_output:
            return
        if not self.review_data:
            # the model output failed to parse: an empty review must never read as "nothing found"
            get_logger().warning("No parsed review data available, skipping review verdict")
            return
        try:
            event, reason = self._determine_review_verdict(self.review_data)
        except Exception as e:
            # a verdict must never block the PR flow - fall back to a non-blocking comment
            get_logger().exception(f"Failed to determine review verdict, falling back to COMMENT, error: {e}")
            event, reason = "COMMENT", "the review verdict could not be determined"
        # Restating an unchanged verdict adds a second identical review on every push,
        # so only speak up when the standing verdict actually changes.
        resulting_state = VERDICT_EVENT_TO_STATE.get(event)
        current_state = self.git_provider.get_latest_own_review_state()
        if current_state is not None and current_state == resulting_state and not self._is_manual_invocation():
            get_logger().info(f"Review verdict is unchanged ({current_state}), skipping submission")
            return

        get_logger().info(f"Submitting review verdict {event}: {reason}")
        if not self.git_provider.submit_review_verdict(event, f"Automated review: {reason}."):
            get_logger().info(f"Review verdict {event} was not submitted")

    async def _prepare_prediction(self, model: str) -> None:
        output = get_pr_diff(self.git_provider,
                             self.token_handler,
                             model,
                             add_line_numbers_to_hunks=True,
                             disable_extra_lines=False,
                             return_remaining_files=True,)
        if isinstance(output, tuple):
            self.patches_diff, self.remaining_files_list = output
        else:
            self.patches_diff = output
            self.remaining_files_list = []

        if self.patches_diff:
            get_logger().debug(f"PR diff", diff=self.patches_diff)
            self.prediction = await self._get_prediction(model)
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            self.prediction = None

    async def _get_prediction(self, model: str) -> str:
        """
        Generate an AI prediction for the pull request review.

        Args:
            model: A string representing the AI model to be used for the prediction.

        Returns:
            A string representing the AI prediction for the pull request review.
        """
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff  # update diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_review_prompt.user).render(variables)

        response, finish_reason = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )

        return response

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        first_key = 'review'
        last_key = 'security_concerns'
        data = load_yaml(self.prediction.strip(),
                         keys_fix_yaml=["ticket_compliance_check", "estimated_effort_to_review_[1-5]:", "security_concerns:", "key_issues_to_review:",
                                        "relevant_file:", "relevant_line:", "suggestion:"],
                         first_key=first_key, last_key=last_key)
        data, recovered_wrapper = recover_missing_review_wrapper(
            data, require_severity=get_settings().pr_reviewer.get("enable_review_verdict", False))
        if recovered_wrapper:
            get_logger().warning("Recovered review response with a missing top-level 'review' wrapper")

        if not isinstance(data, dict) or not isinstance(data.get("review"), dict):
            get_logger().error("Failed to parse review data", artifact={"data": data})
            return ""

        github_action_output(data, 'review')
        self.review_data = data

        structured_publisher = getattr(self.git_provider, "publish_structured_review", None)
        if callable(structured_publisher):
            # Deep-copy the data: dict(data) is shallow, so structured_data["review"]
            # would alias data["review"], which is mutated right below (key reordering).
            # Hand implementers an isolated snapshot, since the hook is provider-neutral
            # and a provider that defers serialization would observe the mutation.
            structured_data = copy.deepcopy(data)
            details = get_run_details()
            usage = {}
            if details is not None and details.has_token_usage:
                usage = {
                    "prompt_tokens": details.prompt_tokens,
                    "completion_tokens": details.completion_tokens,
                    "total_tokens": details.total_tokens,
                }
            structured_data["usage"] = usage
            structured_publisher(structured_data)

        # move data['review'] 'key_issues_to_review' key to the end of the dictionary
        if 'key_issues_to_review' in data['review']:
            key_issues_to_review = data['review'].pop('key_issues_to_review')
            data['review']['key_issues_to_review'] = key_issues_to_review

        if get_settings().config.publish_output and get_settings().pr_reviewer.get('inline_key_issues', False):
            data = self._publish_key_issues_as_inline_comments(data)

        incremental_review_markdown_text = None
        # Add incremental review section
        if self.incremental.is_incremental:
            last_commit_url = f"{self.git_provider.get_pr_url()}/commits/" \
                              f"{self.git_provider.incremental.first_new_commit_sha}"
            incremental_review_markdown_text = f"Starting from commit {last_commit_url}"

        markdown_text = convert_to_markdown_v2(data, self.git_provider.is_supported("gfm_markdown"),
                                            incremental_review_markdown_text,
                                               git_provider=self.git_provider,
                                               files=self.git_provider.get_diff_files())

        if self.remaining_files_list and get_settings().pr_reviewer.enable_review_coverage_footer:
            displayed_files = self.remaining_files_list[:MAX_REVIEW_COVERAGE_FILES]
            markdown_text += (
                "\n\n<hr>\n\n"
                "⚠️ **Review coverage:** The following files were not included in this review "
                "because of the token budget:\n"
                + "\n".join(f"- `{file}`" for file in displayed_files)
            )
            remaining_count = len(self.remaining_files_list) - len(displayed_files)
            if remaining_count:
                markdown_text += f"\n... and {remaining_count} more"

        # Add help text if gfm_markdown is supported
        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_reviewer.enable_help_text:
            markdown_text += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
            markdown_text += HelpMessage.get_review_usage_guide()
            markdown_text += "\n</details>\n"

        # Output the relevant configurations if enabled
        if get_settings().get('config', {}).get('output_relevant_configurations', False):
            markdown_text += show_relevant_configurations(relevant_section='pr_reviewer')

        # Output the agent run details (model, tokens, time cost) if enabled
        if get_settings().get('config', {}).get('output_run_details', False):
            markdown_text += show_run_details(self.git_provider.is_supported("gfm_markdown"))

        # Add custom labels from the review prediction (effort, security)
        self.set_review_labels(data)

        if markdown_text == None or len(markdown_text) == 0:
            markdown_text = ""

        return markdown_text

    def _build_key_issue_comment(self, issue, diff_files: dict) -> Optional[dict]:
        if not isinstance(issue, dict):
            return None
        relevant_file = (issue.get("relevant_file") or "").strip()
        issue_content = _SUGGESTION_FENCE_RE.sub("```text", (issue.get("issue_content") or "").strip())
        issue_header = (issue.get("issue_header") or "").strip()
        if issue_header.lower() == "possible bug":
            issue_header = "Possible Issue"
        try:
            start_line = int(str(issue.get("start_line", 0)).strip())
            end_line = int(str(issue.get("end_line", 0)).strip())
        except ValueError:
            start_line, end_line = 0, 0

        if not relevant_file or not issue_content or start_line < 1 or end_line < start_line:
            get_logger().warning("Review finding has no usable location, keeping it in the summary",
                                 artifact={"relevant_file": relevant_file, "start_line": start_line,
                                           "end_line": end_line})
            return None

        file = diff_files.get(relevant_file) or diff_files.get(relevant_file.lstrip("/"))
        if file is None:
            get_logger().warning("Review finding points at a file that is not in the diff, "
                                 "keeping it in the summary", artifact={"relevant_file": relevant_file})
            return None
        if not file.head_file or end_line > len(file.head_file.splitlines()):
            get_logger().warning("Review finding points past the end of the file, keeping it in the summary",
                                 artifact={"relevant_file": relevant_file, "start_line": start_line,
                                           "end_line": end_line})
            return None

        relevant_file = file.filename.strip()
        badge = format_severity_badge(issue.get("severity"), self.git_provider.is_supported("gfm_markdown"))
        body = f"{badge}**{issue_header}**\n\n{issue_content}" if issue_header else f"{badge}{issue_content}"
        return {"body": body,
                "relevant_file": relevant_file,
                "relevant_lines_start": start_line,
                "relevant_lines_end": end_line,
                "fallback_to_pr_comment": False}

    def _can_verify_inline_key_issue_publication(self) -> bool:
        return can_verify_inline_comment_publication(self.git_provider)

    def _published_inline_key_issue_fingerprints(self, store: InlineCommentStore,
                                                 fingerprints: set[str]) -> set[str]:
        try:
            for body in self.git_provider.get_recent_inline_comment_bodies():
                store.add_body(body)
        except Exception as e:
            get_logger().warning(
                f"Inline key-issue publishing cannot verify newly published comments, error: {e}; "
                "keeping findings in the review summary")
            return set()
        return {fingerprint for fingerprint in fingerprints if store.seen(fingerprint)}

    def _publish_key_issues_as_inline_comments(self, data: dict) -> dict:
        issues = (data.get("review") or {}).get("key_issues_to_review")
        if not isinstance(issues, list) or not issues:
            return data
        if not self._can_verify_inline_key_issue_publication():
            get_logger().info("Inline key-issue publishing is not verifiable for this provider; "
                              "keeping findings in the review summary")
            return data

        diff_files = {}
        for file in self.git_provider.get_diff_files() or []:
            if not file.filename:
                continue
            path = file.filename.strip()
            diff_files[path] = file
            diff_files.setdefault(path.lstrip("/"), file)
        store = get_inline_comment_store(self.git_provider)
        store.load()
        if store.load_failed:
            get_logger().warning("Inline key-issue publishing cannot verify existing inline comments; "
                                 "keeping findings in the review summary")
            return data
        remaining_issues = []
        candidate_comments = {}
        candidate_issues = {}
        candidate_fingerprints = {}
        candidate_anchors = {}
        published = 0
        self.carried_over_findings = 0
        # When off, a finding already posted on an earlier review is posted again on re-review
        # instead of being deduped, so each review shows every finding it raised.
        dedup_carried_over = get_settings().pr_reviewer.get("dedup_carried_over_key_issues", True)
        for issue in issues:
            try:
                comment = self._build_key_issue_comment(issue, diff_files)
                if comment is None:
                    remaining_issues.append(issue)
                    continue
                fingerprint = key_issue_fingerprint(comment["relevant_file"], comment["body"])
                anchor_fingerprint = key_issue_anchor_fingerprint(
                    comment["relevant_file"], comment["relevant_lines_start"], comment["relevant_lines_end"])
                # The body fingerprint misses a reworded repeat of the same finding, so the
                # anchor is what keeps a re-review from stacking comments on one location.
                if dedup_carried_over and (store.seen(fingerprint) or store.seen(anchor_fingerprint)):
                    published += 1
                    self.carried_over_findings += 1
                    continue
                location_fingerprint = key_issue_location_fingerprint(
                    fingerprint, comment["relevant_lines_start"], comment["relevant_lines_end"])
                if location_fingerprint in candidate_comments:
                    candidate_issues[location_fingerprint].append(issue)
                    continue
                comment["body"] = key_issue_body_with_markers(
                    comment["body"], fingerprint, location_fingerprint,
                    getattr(self.git_provider, "max_comment_chars", None),
                    anchor_fp=anchor_fingerprint)
                candidate_comments[location_fingerprint] = comment
                candidate_issues[location_fingerprint] = [issue]
                candidate_fingerprints[location_fingerprint] = fingerprint
                candidate_anchors[location_fingerprint] = anchor_fingerprint
            except Exception as e:
                get_logger().warning(f"Failed to prepare a review finding for inline publication, error: {e}",
                                     artifact={"issue": issue})
                remaining_issues.append(issue)

        if candidate_comments and self._single_review_submission_enabled():
            # Hand the comments back so run() can post them together with the summary and
            # the verdict; they are only confirmed once that single review succeeds.
            self.deferred_review_comments = list(candidate_comments.values())
            for location_fingerprint in candidate_comments:
                store.add(candidate_fingerprints[location_fingerprint])
                store.add(location_fingerprint)
                store.add(candidate_anchors[location_fingerprint])
                published += len(candidate_issues[location_fingerprint])
        elif candidate_comments:
            try:
                self.git_provider.publish_code_suggestions(list(candidate_comments.values()))
            except Exception as e:
                locations = [{"relevant_file": comment["relevant_file"],
                              "start_line": comment["relevant_lines_start"],
                              "end_line": comment["relevant_lines_end"]}
                             for comment in candidate_comments.values()]
                get_logger().warning(
                    f"Failed to publish review findings as inline comments, error: {e}",
                    artifact={"locations": locations})
            verified_locations = self._published_inline_key_issue_fingerprints(store, set(candidate_comments))
            for location_fingerprint, comment in candidate_comments.items():
                issues_for_location = candidate_issues[location_fingerprint]
                if location_fingerprint in verified_locations:
                    store.add(candidate_fingerprints[location_fingerprint])
                    store.add(location_fingerprint)
                    store.add(candidate_anchors[location_fingerprint])
                    published += len(issues_for_location)
                    continue
                get_logger().warning("Failed to publish a review finding as an inline comment, "
                                     "keeping it in the summary",
                                     artifact={"relevant_file": comment["relevant_file"],
                                               "start_line": comment["relevant_lines_start"],
                                               "end_line": comment["relevant_lines_end"]})
                remaining_issues.extend(issues_for_location)

        if not published:
            return data
        get_logger().info(f"Published {published} review finding(s) as inline comments")

        data = copy.deepcopy(data)
        if remaining_issues:
            data["review"]["key_issues_to_review"] = remaining_issues
        else:
            data["review"].pop("key_issues_to_review", None)
        return data

    def _get_user_answers(self) -> Tuple[str, str]:
        """
        Retrieves the question and answer strings from the discussion messages related to a pull request.

        Returns:
            A tuple containing the question and answer strings.
        """
        question_str = ""
        answer_str = ""

        if self.is_answer:
            discussion_messages = self.git_provider.get_issue_comments()

            # providers return the comments oldest-first. PyGithub's PaginatedList reverses lazily,
            # so prefer it and only materialise the plain lists other providers return.
            newest_first = getattr(discussion_messages, "reversed", None)
            if newest_first is None:
                newest_first = reversed(list(discussion_messages))

            for message in newest_first:
                if "Questions to better understand the PR:" in message.body:
                    question_str = message.body
                elif '/answer' in message.body:
                    answer_str = message.body

                if answer_str and question_str:
                    break

        return question_str, answer_str

    def _get_previous_review_comment(self):
        """
        Get the previous review comment if it exists.
        """
        try:
            if hasattr(self.git_provider, "get_previous_review"):
                return self.git_provider.get_previous_review(
                    full=not self.incremental.is_incremental,
                    incremental=self.incremental.is_incremental,
                )
        except Exception as e:
            get_logger().exception(f"Failed to get previous review comment, error: {e}")

    def _remove_previous_review_comment(self, comment):
        """
        Remove the previous review comment if it exists.
        """
        try:
            if comment:
                self.git_provider.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove previous review comment, error: {e}")

    def _can_run_incremental_review(self) -> bool:
        """
        Checks if we can run incremental review according the various configurations and previous review.
        """
        # checking if running is auto mode but there are no new commits
        if self.is_auto and not self.incremental.first_new_commit_sha:
            get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new commits")
            return False

        if not hasattr(self.git_provider, "get_incremental_commits"):
            get_logger().info(f"Incremental review is not supported for {get_settings().config.git_provider}")
            return False
        if self.incremental.commits_range is None:
            get_logger().info(
                f"Incremental review not initialized for {get_settings().config.git_provider}; "
                f"falling back to full review."
            )
            self.incremental.is_incremental = False
            return False
        # checking if there are enough commits to start the review
        num_new_commits = len(self.incremental.commits_range)
        num_commits_threshold = get_settings().pr_reviewer.minimal_commits_for_incremental_review
        not_enough_commits = num_new_commits < num_commits_threshold
        # checking if the commits are not too recent to start the review
        recent_commits_threshold = datetime.datetime.now() - datetime.timedelta(
            minutes=get_settings().pr_reviewer.minimal_minutes_for_incremental_review
        )
        last_seen_commit_date = (
            self.incremental.last_seen_commit.commit.author.date if self.incremental.last_seen_commit else None
        )
        all_commits_too_recent = (
            last_seen_commit_date > recent_commits_threshold if self.incremental.last_seen_commit else False
        )
        # check all the thresholds or just one to start the review
        condition = any if get_settings().pr_reviewer.require_all_thresholds_for_incremental_review else all
        if condition((not_enough_commits, all_commits_too_recent)):
            get_logger().info(
                f"Incremental review is enabled for {self.pr_url} but didn't pass the threshold check to run:"
                f"\n* Number of new commits = {num_new_commits} (threshold is {num_commits_threshold})"
                f"\n* Last seen commit date = {last_seen_commit_date} (threshold is {recent_commits_threshold})"
            )
            return False
        return True

    def set_review_labels(self, data):
        if not get_settings().config.publish_output:
            return

        if not get_settings().pr_reviewer.require_estimate_effort_to_review:
            get_settings().pr_reviewer.enable_review_labels_effort = False # we did not generate this output
        if not get_settings().pr_reviewer.require_security_review:
            get_settings().pr_reviewer.enable_review_labels_security = False # we did not generate this output

        if (get_settings().pr_reviewer.enable_review_labels_security or
                get_settings().pr_reviewer.enable_review_labels_effort):
            try:
                review_labels = []
                if get_settings().pr_reviewer.enable_review_labels_effort:
                    estimated_effort = data['review']['estimated_effort_to_review_[1-5]']
                    estimated_effort_number = None
                    if isinstance(estimated_effort, str):
                        try:
                            estimated_effort_number = int(estimated_effort.split(',')[0])
                        except ValueError:
                            get_logger().warning(f"Invalid estimated_effort value: {estimated_effort}")
                    elif isinstance(estimated_effort, int):
                        estimated_effort_number = estimated_effort
                    else:
                        get_logger().warning(f"Unexpected type for estimated_effort: {type(estimated_effort)}")
                    if estimated_effort_number is not None:
                        estimated_effort_number = max(1, min(5, int(estimated_effort_number)))
                        review_labels.append(f'Review effort {estimated_effort_number}/5')
                if get_settings().pr_reviewer.enable_review_labels_security and get_settings().pr_reviewer.require_security_review:
                    security_concerns = data['review']['security_concerns']  # yes, because ...
                    security_concerns_bool = 'yes' in security_concerns.lower() or 'true' in security_concerns.lower()
                    if security_concerns_bool:
                        review_labels.append('Possible security concern')

                current_labels = self.git_provider.get_pr_labels(update=True)
                if not current_labels:
                    current_labels = []
                get_logger().debug(f"Current labels:\n{current_labels}")
                if current_labels:
                    current_labels_filtered = [label for label in current_labels if
                                               not label.lower().startswith('review effort') and not label.lower().startswith(
                                                   'possible security concern')]
                else:
                    current_labels_filtered = []
                new_labels = review_labels + current_labels_filtered
                if (current_labels or review_labels) and sorted(new_labels) != sorted(current_labels):
                    get_logger().info(f"Setting review labels:\n{review_labels + current_labels_filtered}")
                    self.git_provider.publish_labels(new_labels)
                else:
                    get_logger().info(f"Review labels are already set:\n{review_labels + current_labels_filtered}")
            except Exception as e:
                get_logger().error(f"Failed to set review labels, error: {e}")

    def auto_approve_logic(self):
        """
        Auto-approve a pull request if it meets the conditions for auto-approval.
        """
        if get_settings().config.enable_auto_approval:
            is_auto_approved = self.git_provider.auto_approve()
            if is_auto_approved:
                get_logger().info("Auto-approved PR")
                self.git_provider.publish_comment("Auto-approved PR")
        else:
            get_logger().info("Auto-approval option is disabled")
            self.git_provider.publish_comment("Auto-approval option for PR-Agent is disabled. "
                                              "You can enable it via a [configuration file](https://github.com/Codium-ai/pr-agent/blob/main/docs/REVIEW.md#auto-approval-1)")
