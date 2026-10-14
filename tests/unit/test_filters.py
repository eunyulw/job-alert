"""Unit tests for the explainable post-ingestion job filter layer."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from job_alert.config import FilterConfig, FilterPriority
from job_alert.filters import FilterDecision, evaluate_filters, normalize_text
from job_alert.models import NormalizedJob

FilterBuilder = Callable[..., FilterConfig]
JobBuilder = Callable[..., NormalizedJob]

# Full-width latin (NFKC folds it to ASCII) used to prove the normalization step runs.
FULLWIDTH_ROBOTICS = "\uff32\uff4f\uff42\uff4f\uff54\uff49\uff43\uff33"
FULLWIDTH_SALES = "\uff33\uff21\uff2c\uff25\uff33"


def test_exclusion_wins_over_inclusion(engineering_job, filter_config) -> None:
    rule = filter_config(
        include_title_keywords=["engineer"],
        exclude_title_keywords=["sales"],
    )
    job = engineering_job(title="Sales Engineer")
    assert evaluate_filters(job, [rule])[0].matched is False
    assert evaluate_filters(job, [rule])[0].reason == "excluded title keyword: sales"


def test_empty_include_lists_mean_no_restriction(engineering_job, filter_config) -> None:
    decision = evaluate_filters(engineering_job(), [filter_config()])[0]
    assert decision.matched is True


def test_disabled_filter_explains_itself_and_never_matches(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(id="production-matches", enabled=False, notify=True)
    decision = evaluate_filters(engineering_job(), [rule])[0]
    assert decision.filter_id == "production-matches"
    assert decision.matched is False
    assert decision.notify is False
    assert decision.reason == "filter disabled: production-matches"
    assert decision.defer_until is None


def test_matching_ignores_unicode_case_and_whitespace(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    normalized = normalize_text(f"  {FULLWIDTH_ROBOTICS}\u3000Soft\tENGINEER ")
    assert normalized == "robotics soft engineer"
    rule = filter_config(include_title_keywords=[f"{FULLWIDTH_ROBOTICS}\u3000soft"])
    decision = evaluate_filters(engineering_job(title="Robotics\n\nSoftware   Engineer"), [rule])[0]
    assert decision.matched is True
    assert decision.reason == "included title keyword: robotics soft"


def test_title_keywords_match_normalized_substrings(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    hit = evaluate_filters(
        engineering_job(), [filter_config(include_title_keywords=["robotics software"])]
    )[0]
    assert hit.matched is True
    assert hit.reason == "included title keyword: robotics software"

    miss = evaluate_filters(
        engineering_job(),
        [filter_config(include_title_keywords=["data scientist", "quant"])],
    )[0]
    assert miss.matched is False
    assert miss.reason == "no included title keyword matched: data scientist, quant"


def test_exclusion_normalizes_its_needle_and_reports_the_configured_term(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(exclude_title_keywords=[FULLWIDTH_SALES])
    decision = evaluate_filters(engineering_job(title="Enterprise Sales Engineer"), [rule])[0]
    assert decision.matched is False
    assert decision.reason == "excluded title keyword: sales"


def test_exclusions_that_do_not_match_leave_the_job_matching(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    decision = evaluate_filters(
        engineering_job(), [filter_config(exclude_title_keywords=["sales"], notify=True)]
    )[0]
    assert decision.matched is True
    assert decision.notify is True
    assert decision.reason == "no excluded title keyword matched"


def test_location_rules_match_normalized_substrings(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    hit = evaluate_filters(engineering_job(), [filter_config(include_locations=["SANTA CLARA"])])[0]
    assert hit.matched is True
    assert hit.reason == "included location: santa clara"

    miss = evaluate_filters(engineering_job(), [filter_config(include_locations=["new york"])])[0]
    assert miss.matched is False
    assert miss.reason == "no included location matched: new york"

    remote = evaluate_filters(
        engineering_job(locations=["Remote - United States"]),
        [filter_config(include_locations=["remote"])],
    )[0]
    assert remote.matched is True
    assert remote.reason == "included location: remote"


@pytest.mark.parametrize(
    ("published", "canonical"),
    [
        ("Remote", "remote"),
        ("remote", "remote"),
        ("TELECOMMUTE", "remote"),
        ("Hybrid", "hybrid"),
        ("OnSite", "on-site"),
        ("On-Site", "on-site"),
        ("on site", "on-site"),
        ("ON-SITE", "on-site"),
    ],
)
def test_workplace_types_compare_against_the_documented_vocabulary(
    published: str, canonical: str, engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(workplace_types=[canonical])
    decision = evaluate_filters(engineering_job(workplace_type=published), [rule])[0]
    assert decision.matched is True
    assert decision.reason == f"included workplace type: {canonical}"


def test_unknown_workplace_labels_still_compare_literally(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    miss = evaluate_filters(
        engineering_job(workplace_type="Field"), [filter_config(workplace_types=["remote"])]
    )[0]
    assert miss.matched is False
    assert miss.reason == "workplace type not included: field"

    literal = evaluate_filters(
        engineering_job(workplace_type="Field"), [filter_config(workplace_types=["Field"])]
    )[0]
    assert literal.matched is True
    assert literal.reason == "included workplace type: field"


def test_missing_workplace_type_never_matches_a_workplace_rule(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(workplace_types=["remote", "hybrid"])
    decision = evaluate_filters(engineering_job(workplace_type=None), [rule])[0]
    assert decision.matched is False
    assert decision.reason == "workplace type unknown, expected one of: remote, hybrid"


def test_department_employment_type_and_description_rules_restrict_matches(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        departments=["engineering"],
        employment_types=["full-time"],
        description_keywords=["robot arms"],
    )
    decision = evaluate_filters(engineering_job(), [rule])[0]
    assert decision.matched is True
    assert decision.reason == "included department: engineering"

    department_miss = evaluate_filters(engineering_job(department="Design"), [rule])[0]
    assert department_miss.matched is False
    assert department_miss.reason == "department not included: design"

    employment_miss = evaluate_filters(
        engineering_job(), [filter_config(employment_types=["internship"])]
    )[0]
    assert employment_miss.matched is False
    assert employment_miss.reason == "employment type not included: full-time"

    internship = evaluate_filters(
        engineering_job(employment_type="Internship"),
        [filter_config(employment_types=["internship"])],
    )[0]
    assert internship.matched is True
    assert internship.reason == "included employment type: internship"

    description_miss = evaluate_filters(
        engineering_job(), [filter_config(description_keywords=["kubernetes"])]
    )[0]
    assert description_miss.matched is False
    assert description_miss.reason == "no included description keyword matched: kubernetes"

    no_description = evaluate_filters(
        engineering_job(description_text=None), [filter_config(description_keywords=["robot"])]
    )[0]
    assert no_description.matched is False

    description_hit = evaluate_filters(
        engineering_job(), [filter_config(description_keywords=["ROBOT"])]
    )[0]
    assert description_hit.matched is True
    assert description_hit.reason == "included description keyword: robot"


def test_company_categories_match_the_supplied_company_record(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(categories=["robotics-autonomy-physical-ai"])
    job = engineering_job()

    hit = evaluate_filters(
        job, [rule], company_categories=["big-tech-ai", "Robotics-Autonomy-Physical-AI"]
    )[0]
    assert hit.matched is True
    assert hit.reason == "included category: robotics-autonomy-physical-ai"

    miss = evaluate_filters(job, [rule], company_categories=["big-tech-ai"])[0]
    assert miss.matched is False
    assert miss.reason == "no included category matched: robotics-autonomy-physical-ai"

    unavailable = evaluate_filters(job, [rule])[0]
    assert unavailable.matched is False
    assert unavailable.reason == "company categories unavailable"


def test_decisions_follow_filter_order_and_are_deterministic(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    filters = [
        filter_config(id="first", include_companies=["NVIDIA"], notify=True),
        filter_config(id="second", enabled=False),
        filter_config(id="third", exclude_title_keywords=["robotics"]),
        filter_config(id="fourth", include_companies=["meta"]),
    ]
    job = engineering_job()
    decisions = evaluate_filters(job, filters)
    assert [decision.filter_id for decision in decisions] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    assert [decision.matched for decision in decisions] == [True, False, False, False]
    assert decisions[0].reason == "company included: nvidia"
    assert decisions[3].reason == "company not included: nvidia"
    assert evaluate_filters(job, filters) == decisions
    assert evaluate_filters(job, []) == []


def test_decision_reports_priority_and_notification_authorization(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    decision = evaluate_filters(
        engineering_job(), [filter_config(id="prod", notify=True, priority="high")]
    )[0]
    assert isinstance(decision, FilterDecision)
    assert decision.filter_id == "prod"
    assert decision.matched is True
    assert decision.notify is True
    assert decision.priority is FilterPriority.HIGH
    assert decision.defer_until is None
    assert decision.reason == "no inclusion restrictions configured"

    dry_run = evaluate_filters(engineering_job(), [filter_config(notify=False)])[0]
    assert dry_run.matched is True
    assert dry_run.notify is False
    assert dry_run.priority is FilterPriority.NORMAL


def test_quiet_hours_defer_matched_decisions_until_the_window_closes(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        notify=True,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        timezone="America/Chicago",
    )
    # 2026-09-19T03:30Z is 2026-09-18T22:30 in Chicago, inside the wrapping quiet window.
    decision = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 9, 19, 3, 30, tzinfo=UTC)
    )[0]
    assert decision.matched is True
    assert decision.notify is True
    assert decision.defer_until == datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def test_quiet_hours_leave_daytime_matches_immediate(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        notify=True,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        timezone="America/Chicago",
    )
    decision = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 9, 19, 18, 0, tzinfo=UTC)
    )[0]
    assert decision.matched is True
    assert decision.defer_until is None


def test_quiet_hours_without_a_timezone_use_utc(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(notify=True, quiet_hours_start="22:00", quiet_hours_end="07:00")
    decision = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 9, 19, 23, 0, tzinfo=UTC)
    )[0]
    assert decision.defer_until == datetime(2026, 9, 20, 7, 0, tzinfo=UTC)

    daytime = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    )[0]
    assert daytime.defer_until is None


def test_naive_now_is_read_as_utc(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(notify=True, quiet_hours_start="22:00", quiet_hours_end="07:00")
    aware = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 9, 19, 23, 0, tzinfo=UTC)
    )[0]
    naive = evaluate_filters(engineering_job(), [rule], now=datetime(2026, 9, 19, 23, 0))[0]
    assert naive == aware
    assert naive.defer_until == datetime(2026, 9, 20, 7, 0, tzinfo=UTC)


def test_quiet_hours_deferral_crosses_a_spring_forward_transition(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        notify=True,
        quiet_hours_start="23:00",
        quiet_hours_end="07:00",
        timezone="America/Chicago",
    )
    # 2026-03-08T06:30Z is 2026-03-08T00:30 CST; the window ends at 07:00 CDT because the
    # clock jumps from 02:00 CST to 03:00 CDT at 08:00Z, so the deferral keeps the wall-clock end.
    decision = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    )[0]
    assert decision.defer_until == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)


def test_quiet_hours_end_inside_a_spring_forward_gap_defers_to_the_jump(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        notify=True,
        quiet_hours_start="23:00",
        quiet_hours_end="02:30",
        timezone="America/Chicago",
    )
    # 02:30 never happens on 2026-03-08 in Chicago; the window ends the moment the clock jumps
    # past it, which is 03:00 CDT (08:00Z).
    decision = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    )[0]
    assert decision.defer_until is not None
    assert abs(decision.defer_until - datetime(2026, 3, 8, 8, 0, tzinfo=UTC)) < timedelta(seconds=1)


def test_quiet_hours_pick_the_next_end_across_a_fall_back_transition(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        notify=True,
        quiet_hours_start="01:00",
        quiet_hours_end="01:45",
        timezone="America/Chicago",
    )
    # 2026-11-01 repeats 01:00-01:59 in Chicago, so 01:45 happens twice: 06:45Z and 07:45Z.
    first_pass = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    )[0]
    assert first_pass.defer_until == datetime(2026, 11, 1, 6, 45, tzinfo=UTC)

    second_pass = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    )[0]
    assert second_pass.defer_until == datetime(2026, 11, 1, 7, 45, tzinfo=UTC)


def test_unmatched_filters_never_defer(
    engineering_job: JobBuilder, filter_config: FilterBuilder
) -> None:
    rule = filter_config(
        notify=True,
        include_title_keywords=["data scientist"],
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        timezone="America/Chicago",
    )
    decision = evaluate_filters(
        engineering_job(), [rule], now=datetime(2026, 9, 19, 3, 30, tzinfo=UTC)
    )[0]
    assert decision.matched is False
    assert decision.defer_until is None


def test_required_title_keywords_must_also_match(engineering_job, filter_config) -> None:
    """Two title lists AND together: an internship AND a robotics or mechanical role."""
    rule = filter_config(
        notify=True,
        include_title_keywords=["robot", "mechanical"],
        require_title_keywords=["intern", "co-op"],
    )

    both = evaluate_filters(engineering_job(title="Mechanical Engineering Intern"), [rule])[0]
    assert both.matched is True and both.notify is True

    not_intern = evaluate_filters(engineering_job(title="Robotics Engineer"), [rule])[0]
    assert not_intern.matched is False
    assert not_intern.reason == "no required title keyword matched: intern, co-op"

    not_engineering = evaluate_filters(engineering_job(title="Finance Intern"), [rule])[0]
    assert not_engineering.matched is False
    assert not_engineering.reason.startswith("no included title keyword matched")


def test_required_title_keywords_alone_restrict_the_title(engineering_job, filter_config) -> None:
    rule = filter_config(require_title_keywords=["co-op"])
    decision = evaluate_filters(engineering_job(title="Controls Co-op"), [rule])[0]
    assert decision.matched is True
    assert decision.reason == "required title keyword: co-op"


def test_required_title_keywords_reject_blank_terms(filter_config) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        filter_config(require_title_keywords=["intern", " "])


def test_countries_keep_only_postings_with_a_us_location(engineering_job, filter_config) -> None:
    rule = filter_config(notify=True, countries=["us"])

    def verdict(*locations: str) -> FilterDecision:
        return evaluate_filters(engineering_job(locations=list(locations)), [rule])[0]

    assert verdict("Santa Clara, CA").matched is True
    assert verdict("Santa Clara, CA").reason == "included country: us"
    # A posting open in several places counts when any one of them is in the US.
    assert verdict("Toronto, ON, CA", "Austin, Texas").matched is True
    assert verdict("London, United Kingdom").reason == "location not in included countries: us"
    # Strict: a place the rule cannot place, or no place at all, is not the US.
    assert verdict("Austin").reason == "location country unknown, expected: us"
    assert verdict().reason == "location unknown, expected: us"


def test_countries_accept_only_known_codes(filter_config) -> None:
    with pytest.raises(ValueError, match="countries"):
        filter_config(countries=["mars"])
    assert filter_config(countries=["US"]).countries == ["US"]
