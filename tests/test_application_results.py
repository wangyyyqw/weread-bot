import unittest
import signal
from argparse import Namespace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from tests.helpers import load_weread_bot


class ApplicationResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = load_weread_bot()

    def test_run_result_status_and_exit_codes(self):
        success = self.bot.RunResult(final_status="success", user_count=1)
        partial = self.bot.RunResult(
            final_status="partial_success",
            user_count=2,
            successful_users=1,
            failed_users=1,
        )
        failed = self.bot.RunResult(
            final_status="failed", user_count=1, failed_users=1
        )
        cancelled = self.bot.RunResult(
            final_status="cancelled", user_count=1, cancelled_users=1
        )

        self.assertEqual(success.exit_code, 0)
        self.assertEqual(partial.exit_code, 1)
        self.assertEqual(failed.exit_code, 1)
        self.assertEqual(cancelled.exit_code, 1)
        self.assertEqual(partial.to_summary_dict()["final_status"], "partial_success")
        self.assertEqual(cancelled.to_summary_dict()["cancelled_users"], 1)

    def test_aggregate_session_results(self):
        stats = self.bot.ReadingSession(successful_reads=1)
        results = [
            self.bot.SessionResult(self.bot.SessionStatus.SUCCESS, stats),
            self.bot.SessionResult(
                self.bot.SessionStatus.FAILED,
                self.bot.ReadingSession(failed_reads=1),
                self.bot.RuntimeErrorCategory.NETWORK,
                "network failed",
            ),
        ]

        result = self.bot.RunResult.from_session_results(results)

        self.assertEqual(result.final_status, "partial_success")
        self.assertEqual(result.successful_users, 1)
        self.assertEqual(result.failed_users, 1)
        self.assertEqual(result.failure_categories, {"network": 1})

    async def test_single_user_failed_session_is_not_success(self):
        config = self.bot.WeReadConfig()
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        self.bot.WeReadApplication._instance = app
        session_result = self.bot.SessionResult(
            self.bot.SessionStatus.FAILED,
            self.bot.ReadingSession(user_name="default", failed_reads=5),
            self.bot.RuntimeErrorCategory.PROTOCOL,
            "too many failures",
        )
        fake_manager = unittest.mock.MagicMock()
        fake_manager.start_reading_session = AsyncMock(return_value=session_result)

        with patch.object(self.bot, "WeReadSessionManager", return_value=fake_manager):
            result = await self.bot.WeReadApplication._run_single_user_session(app)

        self.assertEqual(result.final_status, "failed")
        self.assertEqual(result.exit_code, 1)

    async def test_main_returns_run_result_exit_code(self):
        config = self.bot.WeReadConfig(curl_content="safe")
        args = Namespace(
            mode=None,
            config="unused.yaml",
            verbose=False,
            validate_config=False,
            dry_run=False,
            show_last_run=False,
            gain_only=False,
        )
        fake_app = unittest.mock.MagicMock()
        fake_app.run = AsyncMock(
            return_value=self.bot.RunResult(
                final_status="failed", user_count=1, failed_users=1
            )
        )
        with (
            patch.object(self.bot, "parse_arguments", return_value=args),
            patch.object(
                self.bot,
                "ConfigManager",
                return_value=unittest.mock.MagicMock(config=config),
            ),
            patch.object(self.bot, "setup_logging"),
            patch.object(self.bot, "_validate_runtime_config"),
            patch.object(self.bot, "_validate_curl_configs", new=AsyncMock()),
            patch.object(self.bot, "WeReadApplication", return_value=fake_app),
            patch.object(self.bot, "requests", object()),
            patch.object(self.bot, "httpx", object()),
        ):
            exit_code = await self.bot.main()

        self.assertEqual(exit_code, 1)

    async def test_main_returns_130_for_keyboard_interrupt(self):
        config = self.bot.WeReadConfig(curl_content="safe")
        args = Namespace(
            mode=None,
            config="unused.yaml",
            verbose=False,
            validate_config=False,
            dry_run=False,
            show_last_run=False,
            gain_only=False,
        )
        fake_app = unittest.mock.MagicMock()
        fake_app.run = AsyncMock(side_effect=KeyboardInterrupt)
        with (
            patch.object(self.bot, "parse_arguments", return_value=args),
            patch.object(
                self.bot,
                "ConfigManager",
                return_value=unittest.mock.MagicMock(config=config),
            ),
            patch.object(self.bot, "setup_logging"),
            patch.object(self.bot, "_validate_runtime_config"),
            patch.object(self.bot, "_validate_curl_configs", new=AsyncMock()),
            patch.object(self.bot, "WeReadApplication", return_value=fake_app),
            patch.object(self.bot, "requests", object()),
            patch.object(self.bot, "httpx", object()),
        ):
            exit_code = await self.bot.main()

        self.assertEqual(exit_code, 130)

    async def test_run_notification_combines_reading_and_claimed_reward_once(self):
        config = self.bot.WeReadConfig()
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        result = self.bot.RunResult(
            final_status="success",
            user_count=1,
            successful_users=1,
            total_duration_seconds=2924,
            total_reads=42,
            total_failed_reads=1,
            gain={
                "status": "success",
                "gain_type": 1,
                "claimed": [11],
                "claimed_rewards": ["1天体验卡"],
            },
        )

        service = unittest.mock.MagicMock()
        service.send_notification_async = AsyncMock(return_value=True)
        with patch.object(
            self.bot, "NotificationService", return_value=service
        ):
            await self.bot.WeReadApplication._notify_run_result(app, result)

        service.send_notification_async.assert_awaited_once()
        sent_message = service.send_notification_async.await_args.args[0]
        self.assertIn("阅读时长：48分44秒", sent_message)
        self.assertIn("已领取：1天体验卡", sent_message)
        self.assertNotIn("档位", sent_message)
        self.assertNotIn("11", sent_message)
        self.assertEqual(
            service.send_notification_async.await_args.kwargs["event"],
            self.bot.NotificationEvent.SESSION_SUCCESS,
        )

    async def test_failed_gain_uses_general_notification_event(self):
        config = self.bot.WeReadConfig()
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        result = self.bot.RunResult(
            final_status="success",
            user_count=1,
            successful_users=1,
            gain={"status": "failed", "error": "鉴权失败"},
        )
        service = unittest.mock.MagicMock()
        service.send_notification_async = AsyncMock(return_value=True)

        with patch.object(
            self.bot, "NotificationService", return_value=service
        ):
            await self.bot.WeReadApplication._notify_run_result(app, result)

        service.send_notification_async.assert_awaited_once()
        self.assertEqual(
            service.send_notification_async.await_args.kwargs["event"],
            self.bot.NotificationEvent.GENERAL,
        )

    async def test_reward_only_notification_does_not_include_reading_statistics(self):
        config = self.bot.WeReadConfig()
        config.notification.include_statistics = False
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        result = self.bot.RunResult(
            final_status="success",
            user_count=1,
            successful_users=1,
            gain={
                "status": "success",
                "claimed": [4],
                "claimed_rewards": ["1天体验卡"],
            },
        )
        service = unittest.mock.MagicMock()
        service.send_notification_async = AsyncMock(return_value=True)

        with patch.object(
            self.bot, "NotificationService", return_value=service
        ):
            await self.bot.WeReadApplication._notify_run_result(app, result)

        service.send_notification_async.assert_awaited_once()
        message = service.send_notification_async.await_args.args[0]
        self.assertIn("已领取：1天体验卡", message)
        self.assertNotIn("阅读时长", message)

    async def test_run_single_session_sends_only_after_gain_finishes(self):
        app = object.__new__(self.bot.WeReadApplication)
        app.config = self.bot.WeReadConfig()
        app.execution_type = "normal"
        self.bot.WeReadApplication._instance = app
        result = self.bot.RunResult(
            final_status="success", user_count=1, successful_users=1
        )
        steps = []

        async def finish_gain(_instance, run_result):
            steps.append("gain")
            run_result.gain = {"status": "success", "claimed_rewards": [
                "2天体验卡"
            ]}

        async def notify(_instance, run_result):
            self.assertIn("claimed_rewards", run_result.gain)
            steps.append("notify")

        with (
            patch.object(
                self.bot.WeReadApplication,
                "_derive_web_session_if_configured",
                new=AsyncMock(),
            ),
            patch.object(
                self.bot.WeReadApplication,
                "_run_single_user_session",
                new=AsyncMock(return_value=result),
            ),
            patch.object(
                self.bot.WeReadApplication,
                "_run_gain_if_configured",
                new=AsyncMock(side_effect=finish_gain),
            ),
            patch.object(
                self.bot.WeReadApplication,
                "_notify_run_result",
                new=AsyncMock(side_effect=notify),
            ),
            patch.object(self.bot, "persist_run_history"),
        ):
            await self.bot.WeReadApplication.run_single_session()

        self.assertEqual(steps, ["gain", "notify"])

    async def test_partial_run_uses_session_failure_notification_trigger(self):
        config = self.bot.WeReadConfig()
        config.notification.triggers[
            self.bot.NotificationEvent.SESSION_FAILURE
        ] = False
        config.notification.triggers[
            self.bot.NotificationEvent.MULTI_USER_SUMMARY
        ] = True
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        result = self.bot.RunResult(
            final_status="partial_success", user_count=2,
            successful_users=1, failed_users=1,
        )
        service = unittest.mock.MagicMock()
        service.send_notification_async = AsyncMock(return_value=True)

        with patch.object(
            self.bot, "NotificationService", return_value=service
        ):
            await self.bot.WeReadApplication._notify_run_result(app, result)

        service.send_notification_async.assert_awaited_once()
        self.assertEqual(
            service.send_notification_async.await_args.kwargs["event"],
            self.bot.NotificationEvent.SESSION_FAILURE,
        )

    async def test_multi_user_summary_is_sent_once_in_combined_notification(self):
        config = self.bot.WeReadConfig()
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        result = self.bot.RunResult(
            final_status="success", user_count=2, successful_users=2,
            total_duration_seconds=600, total_reads=14,
            gain={
                "status": "success",
                "claimed_rewards": ["1天体验卡"],
            },
        )
        service = unittest.mock.MagicMock()
        service.send_notification_async = AsyncMock(return_value=True)

        with patch.object(
            self.bot, "NotificationService", return_value=service
        ):
            await self.bot.WeReadApplication._generate_multi_user_summary(
                app, result
            )
            await self.bot.WeReadApplication._notify_run_result(app, result)

        service.send_notification_async.assert_awaited_once()
        message = service.send_notification_async.await_args.args[0]
        self.assertIn("多用户阅读会话总结", message)
        self.assertIn("总阅读时长: 10分0秒", message)
        self.assertIn("已领取：1天体验卡", message)

    def test_multi_user_notification_has_no_duplicate_statistics(self):
        result = self.bot.RunResult(
            final_status="success", user_count=2, successful_users=2,
            total_duration_seconds=600, total_reads=14,
            failure_categories={"network": 1},
        )

        message = self.bot.format_run_result_notification(result)

        self.assertEqual(message.count("总阅读时长"), 1)
        self.assertEqual(message.count("失败分类"), 1)

    async def test_dry_run_validation_failure_does_not_send_notification(self):
        config = self.bot.WeReadConfig(curl_content="safe")
        args = Namespace(
            mode=None,
            config="unused.yaml",
            verbose=False,
            validate_config=False,
            dry_run=True,
            show_last_run=False,
            gain_only=False,
        )
        notification_service = unittest.mock.MagicMock()
        notification_service.send_notification_async = AsyncMock()

        with (
            patch.object(self.bot, "parse_arguments", return_value=args),
            patch.object(
                self.bot,
                "ConfigManager",
                return_value=unittest.mock.MagicMock(config=config),
            ),
            patch.object(self.bot, "setup_logging"),
            patch.object(
                self.bot,
                "_validate_runtime_config",
                side_effect=self.bot.ConfigError("invalid"),
            ),
            patch.object(
                self.bot,
                "NotificationService",
                return_value=notification_service,
            ),
        ):
            with self.assertLogs(level="ERROR"):
                exit_code = await self.bot.main()

        self.assertEqual(exit_code, 1)
        notification_service.send_notification_async.assert_not_awaited()

    def test_signal_requests_shutdown_and_records_interrupt(self):
        with patch.object(self.bot.signal, "signal"):
            app = self.bot.WeReadApplication(self.bot.WeReadConfig())

        app._signal_handler(signal.SIGINT, None)

        self.assertTrue(app.is_shutdown_requested())
        self.assertEqual(app.shutdown_signal, signal.SIGINT)

    def test_application_shutdown_state_is_isolated_per_instance(self):
        with patch.object(self.bot.signal, "signal"):
            first = self.bot.WeReadApplication(self.bot.WeReadConfig())
            second = self.bot.WeReadApplication(self.bot.WeReadConfig())

        first._signal_handler(signal.SIGTERM, None)

        self.assertTrue(first.is_shutdown_requested())
        self.assertFalse(second.is_shutdown_requested())

    async def test_scheduled_runtime_error_continues_to_next_cron(self):
        config = self.bot.WeReadConfig(
            startup_mode="scheduled",
            schedule=self.bot.ScheduleConfig(
                enabled=True,
                cron_expression="* * * * *",
                timezone="UTC",
            ),
        )
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        app.execution_type = "normal"
        app._shutdown_event = self.bot.asyncio.Event()
        app.shutdown_signal = None
        self.bot.WeReadApplication._instance = app
        real_datetime = self.bot.datetime

        class FakeDateTime(real_datetime):
            current = real_datetime(
                2026, 7, 20, 12, 0, tzinfo=self.bot.ZoneInfo("UTC")
            )

            @classmethod
            def now(cls, timezone=None):
                if timezone is None:
                    return cls.current.replace(tzinfo=None)
                return cls.current.astimezone(timezone)

        class FakeCron:
            calls = 0

            def get_next(self, _result_type):
                self.calls += 1
                if self.calls > 1:
                    app._shutdown_event.set()
                return FakeDateTime.current + timedelta(seconds=1)

        async def fake_sleep(seconds, is_cancelled):
            FakeDateTime.current += timedelta(seconds=seconds)
            return not is_cancelled()

        session_attempt = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(self.bot, "datetime", FakeDateTime),
            patch.object(self.bot, "croniter", return_value=FakeCron()),
            patch.object(self.bot, "interruptible_sleep", new=fake_sleep),
            patch.object(
                self.bot.WeReadApplication,
                "run_single_session",
                new=session_attempt,
            ),
            patch.object(self.bot, "persist_run_history") as persist_history,
        ):
            with self.assertLogs(level="WARNING"):
                result = await app._run_scheduled_mode()

        self.assertEqual(result.final_status, "failed")
        self.assertEqual(result.failed_users, 0)
        self.assertEqual(result.failure_categories, {"unknown": 1})
        self.assertEqual(session_attempt.await_count, 1)
        persist_history.assert_called_once()

    async def test_daemon_runtime_error_waits_and_persists(self):
        config = self.bot.WeReadConfig(
            startup_mode="daemon",
            daemon=self.bot.DaemonConfig(
                enabled=True,
                session_interval="2",
                max_daily_sessions=3,
            ),
        )
        app = object.__new__(self.bot.WeReadApplication)
        app.config = config
        app.execution_type = "normal"
        app._shutdown_event = self.bot.asyncio.Event()
        app.shutdown_signal = None
        self.bot.WeReadApplication._instance = app
        self.bot.WeReadApplication._daily_session_count = 0
        self.bot.WeReadApplication._last_session_date = None
        sleep_calls = []

        async def session_attempt():
            if session_mock.await_count == 1:
                raise RuntimeError("boom")
            app._shutdown_event.set()
            return self.bot.RunResult(
                final_status="cancelled",
                user_count=1,
                cancelled_users=1,
            )

        async def fake_sleep(seconds, _is_cancelled):
            sleep_calls.append(seconds)
            app._shutdown_event.set()
            return False

        session_mock = AsyncMock(side_effect=session_attempt)
        with (
            patch.object(
                self.bot.WeReadApplication,
                "run_single_session",
                new=session_mock,
            ),
            patch.object(self.bot, "interruptible_sleep", new=fake_sleep),
            patch.object(self.bot, "persist_run_history") as persist_history,
        ):
            with self.assertLogs(level="WARNING"):
                result = await app._run_daemon_mode()

        self.assertEqual(result.final_status, "failed")
        self.assertEqual(result.failed_users, 0)
        self.assertEqual(result.failure_categories, {"unknown": 1})
        self.assertEqual(session_mock.await_count, 1)
        self.assertEqual(sleep_calls, [120])
        persist_history.assert_called_once()


if __name__ == "__main__":
    unittest.main()
