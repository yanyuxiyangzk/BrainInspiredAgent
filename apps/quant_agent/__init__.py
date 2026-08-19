"""Quant application composition root and built-in deterministic Fake Skills."""

from apps.quant_agent.daily_review import DAILY_REVIEW_WORKFLOW
from apps.quant_agent.daily_review_app import DailyReviewApp, DailyReviewResult
from apps.quant_agent.delivery import DeliveryResult, InsightDeliveryService
from apps.quant_agent.fake_skills import (
    MARKET_CAPABILITY,
    NOTIFICATION_CAPABILITY,
    SUMMARY_CAPABILITY,
    FakeMarketRead,
    FakeSkillBundle,
    FakeSummary,
    LocalNotification,
    NotificationRecord,
    fake_capability_contracts,
    fake_skill_manifests,
    install_fake_skills,
)
from apps.quant_agent.insights import InsightExplanation, MarketInsight, MarketInsightQuery
from apps.quant_agent.market_summary import MARKET_SUMMARY_WORKFLOW
from apps.quant_agent.market_summary_app import MarketSummaryApp, MarketSummaryResult

__all__ = [
    "DAILY_REVIEW_WORKFLOW",
    "MARKET_CAPABILITY",
    "MARKET_SUMMARY_WORKFLOW",
    "NOTIFICATION_CAPABILITY",
    "SUMMARY_CAPABILITY",
    "DailyReviewApp",
    "DailyReviewResult",
    "DeliveryResult",
    "FakeMarketRead",
    "FakeSkillBundle",
    "FakeSummary",
    "InsightDeliveryService",
    "InsightExplanation",
    "LocalNotification",
    "MarketInsight",
    "MarketInsightQuery",
    "MarketSummaryApp",
    "MarketSummaryResult",
    "NotificationRecord",
    "fake_capability_contracts",
    "fake_skill_manifests",
    "install_fake_skills",
]
