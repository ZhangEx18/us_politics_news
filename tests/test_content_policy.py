"""内容政策测试 — content_policy.py"""

from content_policy import is_routine_notice


def test_routine_notice_detects_comment_and_hearing_items():
    assert is_routine_notice("FTC Seeks Public Comment on Proposed Rule")
    assert is_routine_notice("USTR Solicits Comments on Section 301 Review")
    assert is_routine_notice("Agency Extends Public Comment Period for Policy Statement")
    assert is_routine_notice("Commission Schedules Public Hearing on Merger")
    assert is_routine_notice("FTC Publishes Price Transparency FAQs for Auto Dealers")
    assert is_routine_notice("FTC Withdraws Obsolete Policy Statement")
    assert is_routine_notice("FTC Announces 2027 Telemarketer Fees to Access Registry")
    assert is_routine_notice("Agency Notice of Proposed Rulemaking on Fees")


def test_routine_notice_ignores_real_news():
    assert not is_routine_notice("FTC, States Sue Amazon Over Secret Ad Surcharge Scheme")
    assert not is_routine_notice("House Votes to End Iran War")
    assert not is_routine_notice("Fed Holds Interest Rates Steady")
    assert not is_routine_notice("")
    assert not is_routine_notice(None)


def test_routine_notice_matches_chinese_titles():
    assert is_routine_notice("FTC 就拟议规则公开征求意见")
