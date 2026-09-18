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


def test_fomc_statement_and_projections_are_routine_notices():
    assert is_routine_notice("Federal Reserve issues FOMC statement")
    assert is_routine_notice(
        "Federal Reserve Board and Federal Open Market Committee release economic projections from the September 15-16 FOMC meeting"
    )
    assert is_routine_notice("美联储发布FOMC声明")


def test_market_reaction_headline_is_not_routine_notice():
    assert not is_routine_notice("Fed signals possible rate cut as inflation cools")


def test_readout_release_is_routine_notice():
    assert is_routine_notice("冯德莱恩与泽连斯基通话后发布通报")
    assert is_routine_notice("Commission readout of the call with Zelenskyy")
    assert not is_routine_notice("警方发布通报称已抓获嫌疑人")


def test_bureaucratic_notice_keywords_are_routine():
    assert is_routine_notice("国家农村卫生信息交换所项目补充资金通知")
    assert not is_routine_notice("美国众议院通过对俄新制裁法案")
