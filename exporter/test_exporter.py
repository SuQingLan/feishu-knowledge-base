import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import devinsync_feishu_chat_local as exporter
import incremental_export


def message(mid, **extra):
    return {"message_id": mid, "chat_id": "oc_test", "msg_type": "text",
            "content": mid, "create_time": "2026-10-01T00:00:00Z", **extra}


def page(messages, more=False, token=None):
    return {"ok": True, "data": {"messages": messages, "has_more": more, "page_token": token}}


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = exporter.ensure_output_layout(Path(self.temp.name))
        self.config = {**exporter.default_config(), "chat_id": "oc_test", "tail_pages": 0,
                       "download_attachments": False, "request_interval_seconds": 0}

    def test_timezone_and_incremental_start(self):
        self.assertEqual(exporter.parse_time("2026-10-01T08:00:00+08:00"),
                         exporter.parse_time("2026-10-01T00:00:00Z"))
        self.assertEqual(exporter.parse_time("2026-10-01T00:00:00Z").hour, 0)
        self.assertEqual(exporter.get_sync_start({"a": message("a")}, None, "2020-01-01"),
                         "2026-09-30T23:58:00Z")

    def test_current_cli_markers_and_mentions(self):
        msg = message("a", msg_type="post", content='![Image](img_1) <audio key="file_1"/> ![Image](img_2)',
                      mentions=[{"id": "ou_1", "name": "user"}])
        refs = exporter.extract_attachment_candidates(msg)
        self.assertEqual({r["resource_key"]: r["resource_type"] for r in refs},
                         {"img_1": "image", "img_2": "image", "file_1": "file"})
        self.assertEqual(exporter.extract_mentions(msg)[0]["mention_id"], "ou_1")

    def test_bad_response_is_not_empty_success(self):
        with self.assertRaises(RuntimeError):
            exporter.payload_messages({"ok": True, "data": {"items": []}})

    def test_empty_page_with_cursor_does_not_end_export(self):
        with patch.object(exporter, "run_page", side_effect=[page([], True, "next"), page([message("after_empty")])]) as read:
            result = exporter.sync_once(self.config, self.paths, None)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(result["total_messages"], 1)
        self.assertIn("after_empty", exporter.load_records(self.paths))

    def test_saved_page_survives_next_page_failure(self):
        with patch.object(exporter, "run_page", side_effect=[page([message("a")], True, "next"), RuntimeError("offline")]):
            with self.assertRaises(RuntimeError):
                exporter.sync_once(self.config, self.paths, None)
        self.assertIn("a", exporter.load_records(self.paths))
        self.assertEqual(exporter.read_json(self.paths["state"], {})["status"], "failed")

    def test_full_history_and_thread_pagination(self):
        exporter.write_json(self.paths["messages_by_id"], {"old": message("old")})
        self.config["full_history"] = True
        calls = []
        def read_group(config, start, token, sort="asc"):
            calls.append(start)
            return page([message("root", thread_id="omt_test")])
        with patch.object(exporter, "run_page", side_effect=read_group), patch.object(exporter, "run_cli", side_effect=[
            page([message("reply1")], True, "next"), page([message("reply2")])
        ]) as cli:
            result = exporter.sync_once(self.config, self.paths, None)
        self.assertEqual(calls, [None])
        self.assertEqual(result["total_messages"], 4)
        self.assertEqual(result["thread_messages_fetched"], 2)
        self.assertIn("next", cli.call_args_list[1].args[1])

    def test_cli_order_and_download_contract(self):
        with patch.object(exporter, "run_cli", return_value=page([])) as cli:
            exporter.run_page(self.config, None, None)
        self.assertIn("--order", cli.call_args.args[1])
        self.assertNotIn("--sort", cli.call_args.args[1])
        def download(config, args, cwd):
            relative = args[args.index("--output") + 1]
            self.assertFalse(Path(relative).is_absolute())
            self.assertEqual(args[args.index("--type") + 1], "file")
            (cwd / relative).write_bytes(b"example")
            return {"ok": True, "data": {"saved_path": relative}}
        with patch.object(exporter, "run_cli", side_effect=download):
            _, path = exporter.download_message_resource(self.config, self.paths, "om_1", "file_1", "audio")
        self.assertEqual(path.read_bytes(), b"example")

    def test_cross_chat_directory_rejected(self):
        exporter.write_json(self.paths["messages_by_id"], {"a": message("a", chat_id="oc_other")})
        with self.assertRaisesRegex(RuntimeError, "其他群"):
            exporter.sync_once(self.config, self.paths, None)

    def test_bounded_increment_checks_old_thread_and_keeps_boundary_reply(self):
        exporter.write_json(self.paths["messages_by_id"], {"old": message("old", thread_id="omt_old")})
        self.config.update(thread_start="2026-10-06T14:41:00Z", sync_end_time="2026-10-09T00:00:00Z")
        replies = page([message("future", create_time="2026-10-10T00:00:00Z"),
                        message("boundary", create_time="2026-10-06T14:41:00Z"),
                        message("older", create_time="2026-10-06T14:40:00Z")], True, "more-old")
        with patch.object(exporter, "run_page", return_value=page([])), patch.object(exporter, "run_cli", return_value=replies) as cli:
            result = exporter.sync_once(self.config, self.paths, self.config["thread_start"])
        self.assertEqual(cli.call_count, 1)
        self.assertIn("desc", cli.call_args.args[1])
        self.assertEqual(set(exporter.load_records(self.paths)), {"old", "boundary"})
        self.assertEqual(result["newest_time"], "2026-10-06T14:41:00Z")
        with patch.object(exporter, "run_cli") as cli:
            exporter.sync_threads(self.config, self.paths, exporter.load_records(self.paths))
        cli.assert_not_called()

    def test_separate_increment_preserves_base_and_refuses_wrong_resume(self):
        base = Path(self.temp.name)/'base'
        dest = Path(self.temp.name)/'increment'
        base.mkdir()
        original = message('old', create_time='2026-10-06T14:41:00Z')
        exporter.write_json(base/'messages.json', [original])
        exporter.write_json(base/'messages_by_id.json', {'old':original})
        before = (base/'messages.json').read_bytes()
        def sync(config, paths, start):
            self.assertEqual(start, '2026-10-06T14:41:00Z')
            self.assertEqual(config['thread_start'], start)
            self.assertEqual(config['sync_end_time'], exporter.read_json(dest/'increment_manifest.json',{})['end'])
            records = exporter.read_json(paths['messages_by_id'],{})
            records['new'] = message('new', create_time='2026-10-07T00:00:00Z')
            exporter.write_json(paths['messages_by_id'],records)
            return {}
        with patch.object(incremental_export.e, 'sync_once', side_effect=sync):
            incremental_export.main(base,dest)
        self.assertEqual((base/'messages.json').read_bytes(),before)
        self.assertEqual([r['message_id'] for r in exporter.read_json(dest/'new_messages.json',[])],['new'])
        self.assertEqual(exporter.read_json(dest/'messages_by_id.json',{})['old']['message_id'],'old')
        other = Path(self.temp.name)/'other'
        other.mkdir()
        exporter.write_json(other/'messages.json', [original])
        with self.assertRaisesRegex(ValueError,'another base'):
            incremental_export.main(other,dest)

    def test_next_increment_resolves_cached_attachment_to_original(self):
        root = Path(self.temp.name)
        original = root/'original'
        previous = root/'previous'
        dest = root/'next'
        cached = original/'attachments'/'old'/'image.bin'
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b'image')
        previous.mkdir()
        row = message('old', attachments=[{'storage_path':'../original/attachments/old/image.bin',
                                             'download_status':'downloaded'}])
        exporter.write_json(previous/'messages.json',[row])
        exporter.write_json(previous/'messages_by_id.json',{'old':row})
        def sync(config,paths,start):
            seeded = exporter.read_json(paths['messages_by_id'],{})['old']
            self.assertEqual((dest/seeded['attachments'][0]['storage_path']).resolve(),cached.resolve())
            return {}
        with patch.object(incremental_export.e,'sync_once',side_effect=sync):
            incremental_export.main(previous,dest)

    def test_previous_cutoff_and_explicit_end_are_frozen(self):
        base = Path(self.temp.name)/'base'
        dest = Path(self.temp.name)/'next'
        base.mkdir()
        row = message('old')
        exporter.write_json(base/'messages.json', [row])
        exporter.write_json(base/'messages_by_id.json', {'old':row})
        exporter.write_json(base/'increment_manifest.json', {'end':'2026-10-09T12:21:56Z'})
        with patch.object(incremental_export.e, 'sync_once', return_value={}) as sync:
            incremental_export.main(base, dest, end_time='2026-10-11T12:25:56Z')
        self.assertEqual(sync.call_args.args[2], '2026-10-09T12:21:56Z')
        self.assertEqual(sync.call_args.args[0]['sync_end_time'], '2026-10-11T12:25:56Z')
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            incremental_export.main(base, dest, end_time='2026-10-12T12:25:56Z')


if __name__ == "__main__":
    unittest.main()
