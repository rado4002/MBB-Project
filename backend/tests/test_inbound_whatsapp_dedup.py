import inspect
import sys
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from types import ModuleType


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

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.message import Message
from app.modules.m1_gateway.service import ProcessedInbound, process_inbound
from app.schemas.common import ContentType
from app.schemas.messages import InboundMessageRequest, validate_whatsapp_message_id_value
from app.tasks import m1


WHATSAPP_ID_INDEX = "uq_messages_inbound_whatsapp_message_id"
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic/versions/d1e2f3a4b5c6_add_inbound_whatsapp_uniqueness.py"
)


def _inbound_payload(whatsapp_message_id: str):
    values = {
        "customer_phone": "+243812345678",
        "content": "Mbote",
        "content_type": "text",
        "timestamp": datetime.now(timezone.utc),
        "whatsapp_message_id": whatsapp_message_id,
    }
    values["message_id"] = uuid.uuid4()
    values["content_type"] = ContentType.text
    return InboundMessageRequest(**values)


class SchemaModelMigrationTests(unittest.TestCase):
    def test_whatsapp_message_id_validation(self):
        for invalid_id in ("", "   ", " leading", "trailing ", "x" * 101):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaises(ValidationError):
                    _inbound_payload(invalid_id)

        for valid_id in ("x", "AbC-_.:123", "x" * 100):
            with self.subTest(valid_id=valid_id):
                payload = _inbound_payload(valid_id)
                self.assertEqual(payload.whatsapp_message_id, valid_id)

    def test_baileys_schema_uses_the_same_exact_id_validator(self):
        api_source = (Path(__file__).parents[1] / "app/api/v1/messages.py").read_text()
        self.assertIn("whatsapp_message_id: str = Field(..., min_length=1, max_length=100)", api_source)
        self.assertIn("return validate_whatsapp_message_id_value(v)", api_source)
        for invalid_id in ("   ", " leading", "trailing "):
            with self.assertRaises(ValueError):
                validate_whatsapp_message_id_value(invalid_id)

    def test_model_contains_named_partial_unique_inbound_index(self):
        index = next(index for index in Message.__table__.indexes if index.name == WHATSAPP_ID_INDEX)
        self.assertTrue(index.unique)
        self.assertEqual([column.name for column in index.columns], ["whatsapp_message_id"])
        predicate = str(index.dialect_options["postgresql"]["where"])
        self.assertIn("direction = 'inbound'", predicate)
        self.assertIn("whatsapp_message_id IS NOT NULL", predicate)
        self.assertIn("btrim(whatsapp_message_id) <> ''", predicate)

    def test_migration_is_single_safe_revision_with_preflight_and_exact_index(self):
        source = MIGRATION_PATH.read_text()
        self.assertIn('down_revision: Union[str, None] = "c7d8e9f0a1b2"', source)
        self.assertIn("HAVING COUNT(*) > 1", source)
        self.assertIn("manual review is required", source)
        self.assertIn("op.create_index(", source)
        self.assertIn("unique=True", source)
        self.assertIn(WHATSAPP_ID_INDEX, source)
        self.assertIn("direction = 'inbound'", source)
        self.assertIn("btrim(whatsapp_message_id) <> ''", source)
        self.assertIn("op.drop_index(INDEX_NAME", source)
        lowered = source.lower()
        self.assertNotIn(" delete ", lowered)
        self.assertNotIn(" update ", lowered)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, execute_result=None):
        self.execute_result = execute_result
        self.execute_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.add_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _ScalarResult(self.execute_result)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    def add(self, _value):
        self.add_calls += 1


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class _Task:
    def __init__(self):
        self.request = SimpleNamespace(retries=0)
        self.retry = Mock(side_effect=RuntimeError("retry-called"))


class _ConstraintViolation(Exception):
    def __init__(self, constraint_name):
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


def _dependency_patches(session, process_result=None, process_error=None):
    import app.adapters as adapters
    import app.database as database
    import app.modules.m1_gateway.service as service
    import app.modules.m1_gateway.session_cache as session_cache

    async def fake_process_inbound(**_kwargs):
        if process_error is not None:
            raise process_error
        return process_result

    stack = ExitStack()
    stack.enter_context(patch.object(database, "async_session_factory", lambda: _SessionContext(session)))
    stack.enter_context(patch.object(service, "process_inbound", fake_process_inbound))
    stack.enter_context(patch.object(service, "persist_outbound", Mock(side_effect=AssertionError("outbound called"))))
    stack.enter_context(patch.object(session_cache, "get_session", Mock(side_effect=AssertionError("cache read called"))))
    stack.enter_context(patch.object(session_cache, "save_session", Mock(side_effect=AssertionError("cache write called"))))
    stack.enter_context(patch.object(adapters, "get_ai_adapter", Mock(side_effect=AssertionError("AI called"))))
    stack.enter_context(patch.object(m1, "_dispatch_maps_fanout", Mock(side_effect=AssertionError("MAPS called"))))
    stack.enter_context(patch.object(m1, "_send_safe", Mock(side_effect=AssertionError("messaging called"))))
    return stack


async def _run_process(task):
    return await m1._process(
        task=task,
        message_id=str(uuid.uuid4()),
        customer_phone="+243812345678",
        content="Mbote",
        content_type="text",
        timestamp=datetime.now(timezone.utc).isoformat(),
        whatsapp_message_id="WA-authoritative-id",
    )


class DuplicateM1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_service_existing_duplicate_returns_before_mutation(self):
        existing = SimpleNamespace(
            message_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            language="fr",
        )
        session = _Session(execute_result=existing)
        result = await process_inbound(
            session=session,
            customer_phone="+243812345678",
            content="Mbote",
            content_type="text",
            timestamp=datetime.now(timezone.utc),
            whatsapp_message_id="WA-authoritative-id",
            message_id=uuid.uuid4(),
        )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.existing_message_id, existing.message_id)
        self.assertEqual(session.execute_calls, 1)
        self.assertEqual(session.add_calls, 0)

    async def test_existing_duplicate_rolls_back_without_downstream(self):
        session = _Session()
        task = _Task()
        existing_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        duplicate = ProcessedInbound(
            customer_phone="+243812345678",
            conversation_id=conversation_id,
            message_id=uuid.uuid4(),
            language="fr",
            is_duplicate=True,
            existing_message_id=existing_id,
            whatsapp_message_id="WA-authoritative-id",
        )
        with _dependency_patches(session, process_result=duplicate):
            result = await _run_process(task)

        self.assertEqual(result["status"], "duplicate_ignored")
        self.assertEqual(result["existing_message_id"], str(existing_id))
        self.assertEqual(result["conversation_id"], str(conversation_id))
        self.assertEqual(session.rollback_calls, 1)
        self.assertEqual(session.commit_calls, 0)
        task.retry.assert_not_called()

    async def test_named_unique_conflict_rolls_back_without_retry_or_downstream(self):
        existing = SimpleNamespace(message_id=uuid.uuid4(), conversation_id=uuid.uuid4())
        session = _Session(execute_result=existing)
        task = _Task()
        error = IntegrityError("insert", {}, _ConstraintViolation(WHATSAPP_ID_INDEX))
        with _dependency_patches(session, process_error=error):
            result = await _run_process(task)

        self.assertEqual(result["status"], "duplicate_ignored")
        self.assertEqual(result["existing_message_id"], str(existing.message_id))
        self.assertEqual(result["conversation_id"], str(existing.conversation_id))
        self.assertEqual(session.rollback_calls, 1)
        task.retry.assert_not_called()

    async def test_unrelated_integrity_error_rolls_back_and_retries(self):
        session = _Session()
        task = _Task()
        error = IntegrityError("insert", {}, _ConstraintViolation("some_other_constraint"))
        with _dependency_patches(session, process_error=error):
            with self.assertRaisesRegex(RuntimeError, "retry-called"):
                await _run_process(task)

        self.assertEqual(session.rollback_calls, 1)
        task.retry.assert_called_once()

    def test_duplicate_branch_precedes_all_downstream_steps(self):
        source = inspect.getsource(m1._process)
        duplicate_position = source.index("if inbound.is_duplicate")
        for downstream_marker in (
            "get_session(conv_id)",
            "ai.generate(",
            "persist_outbound(",
            "_dispatch_maps_fanout(",
            "detect_qualification_signals",
            "save_session(",
            "_send_safe(",
        ):
            self.assertLess(duplicate_position, source.index(downstream_marker))


if __name__ == "__main__":
    unittest.main()
