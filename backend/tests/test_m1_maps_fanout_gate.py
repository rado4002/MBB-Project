import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock


celery_module = ModuleType("celery")
celery_module.Task = type("Task", (), {})
sys.modules["celery"] = celery_module

fake_celery_app = SimpleNamespace(
    send_task=Mock(),
    task=lambda *args, **kwargs: lambda function: function,
)
celery_app_module = ModuleType("app.tasks.celery_app")
celery_app_module.celery_app = fake_celery_app
celery_app_module.run_async = Mock()
sys.modules["app.tasks.celery_app"] = celery_app_module

from app.tasks import m1


def _dispatch() -> None:
    m1._dispatch_maps_fanout(
        conversation_id="conversation-id",
        message_id="message-id",
        content="test content",
        language="fr",
        content_type="text",
    )


def test_maps_fanout_disabled_does_not_dispatch(monkeypatch):
    send_task = Mock()
    monkeypatch.setattr(m1.settings, "m1_maps_fanout_enabled", False)
    monkeypatch.setattr(m1.celery_app, "send_task", send_task)

    _dispatch()

    send_task.assert_not_called()


def test_maps_fanout_enabled_preserves_dispatch(monkeypatch):
    send_task = Mock()
    monkeypatch.setattr(m1.settings, "m1_maps_fanout_enabled", True)
    monkeypatch.setattr(m1.celery_app, "send_task", send_task)

    _dispatch()

    send_task.assert_called_once()
    args, kwargs = send_task.call_args
    assert args[0] == "app.tasks.maps.tag_event"
    assert kwargs["queue"] == "maps"
