"""Notification delivery: shared message types, burst grouping, and provider notifiers."""

from job_alert.notifications.base import (
    AlertBatch,
    AlertEvent,
    DeliveryResult,
    NotificationConfigurationError,
    NotificationMessage,
    Notifier,
    group_job_events,
)
from job_alert.notifications.macos import MacOSNotifier
from job_alert.notifications.ntfy import NtfyNotifier
from job_alert.notifications.pushover import PushoverNotifier

__all__ = [
    "AlertBatch",
    "AlertEvent",
    "DeliveryResult",
    "MacOSNotifier",
    "NotificationConfigurationError",
    "NotificationMessage",
    "Notifier",
    "NtfyNotifier",
    "PushoverNotifier",
    "group_job_events",
]
