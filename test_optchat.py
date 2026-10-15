"""OptChat storage, rollback order, hook ingestion, MCP, and compaction tests.

All model calls are fakes. This suite neither contacts a model nor changes a
real Codex installation or memory directory.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "codex/plugins/optchat/scripts"
sys.path.insert(0, str(SCRIPTS))
import optchat
import optchat_memory as memory


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optchat-test-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OPTCHAT_DIR": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_short_messages_and_automatic_merge_preserve_words(self):
        with memory.Store(self.root) as store:
            store.append("user", "  Keep caf\u00e9 and \u6771\u4eac.\nExactly.  ", date="2026-10-09T12:30:00+03:00")
            store.append("unii", "Understood.")
            self.assertEqual(store.node(0, 0), "user:   Keep caf\u00e9 and \u6771\u4eac.\nExactly.  ")
            self.assertEqual(store.node(1, 0), store.node(0, 0) + "\n" + store.node(0, 1))
            self.assertEqual(store.view, [[0, 0], [0, 1]])
            self.assertNotIn("2026-10-09", store.render())
            self.assertEqual(optchat.zoom(store, 0)["text"], "  Keep caf\u00e9 and \u6771\u4eac.\nExactly.  ")

    def test_long_unicode_input_is_split_without_loss(self):
        text = "\u6771\u4eac\n\U0001f680  " * 10000
        with memory.Store(self.root) as store:
            ids = store.append("user", text)
            self.assertGreater(len(ids), 1)
            self.assertEqual("".join(store.message(i)["text"] for i in ids), text)
            self.assertTrue(all(store.message(i)["size"] <= 30000 for i in ids))
            self.assertTrue(all(store.node(0, i) is None for i in ids))
            self.assertNotIn(text[:50], store.render())

    def test_only_tool_output_is_clipped(self):
        text = "HEAD" + "\u00e9" * 40000 + "TAIL"
        with memory.Store(self.root) as store:
            ids = store.append("echo", text)
            result = store.message(ids[0])["text"]
            self.assertEqual(len(result), 30000)
            self.assertTrue(result.startswith("HEAD") and result.endswith("TAIL"))
            notes = store.append("note", text)
            self.assertEqual("".join(store.message(i)["text"] for i in notes), text)

    def test_nodes_are_logged_once_and_log_prefix_never_changes(self):
        with memory.Store(self.root) as store:
            store.append("user", "a" * 600)
            store.put(0, 0, "user: original summary")
            files = {p: p.read_bytes() for p in self.root.glob("*/*.jsonl")}
            self.assertFalse(store.put(0, 0, "attempted overwrite"))
            store.append("unii", "second")
            for path, before in files.items():
                self.assertTrue(path.read_bytes().startswith(before))

    def test_concurrent_writers_serialize_and_deduplicate(self):
        code = ("import sys; from optchat_memory import Store; "
                "store = Store(sys.argv[1]); "
                "store.append('user', sys.argv[2], source=sys.argv[2]); store.close()")
        env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root), str(i % 4)],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for i in range(8)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode())
        with memory.Store(self.root) as store:
            self.assertEqual(store.total, 4)
            self.assertEqual({store.message(i)["text"] for i in range(4)}, {"0", "1", "2", "3"})
            self.assertEqual(store.view, [[0, i] for i in range(4)])

    def test_saved_view_survives_restart_and_index_rebuild(self):
        with memory.Store(self.root) as store:
            for i in range(8):
                store.append("user", str(i))
            store.view = [[2, 0], [1, 2], [0, 6], [0, 7]]
            memory.atomic_json(self.root / "view.json", store.view)
            view = (self.root / "view.json").read_bytes()
            compact = (self.root / "view-state.json").read_bytes()
        (self.root / "index.sqlite3").unlink()
        with memory.Store(self.root) as store:
            self.assertEqual(store.view, [[2, 0], [1, 2], [0, 6], [0, 7]])
            self.assertEqual(store.total, 8)
            self.assertEqual((self.root / "view.json").read_bytes(), view)
            self.assertEqual((self.root / "view-state.json").read_bytes(), compact)

    def test_missing_saved_view_is_reported_not_rebuilt(self):
        with memory.Store(self.root) as store:
            store.append("user", "first")
        (self.root / "view.json").unlink()
        with self.assertRaisesRegex(ValueError, "never rebuilt"):
            memory.Store(self.root)

    def test_crash_after_log_append_preserves_view_prefix(self):
        with memory.Store(self.root) as store:
            for i in range(4):
                store.append("user", str(i))
            saved = [[1, 0], [0, 2], [0, 3]]
            memory.atomic_json(self.root / "view.json", saved)
            store.append("user", "new")
            memory.atomic_json(self.root / "view.json", saved)
        with memory.Store(self.root) as store:
            self.assertEqual(store.view, saved + [[0, 4]])

    def test_partial_log_is_not_silently_deleted(self):
        with memory.Store(self.root) as store:
            store.append("user", "durable")
        path = next((self.root / "main").glob("*.jsonl"))
        with path.open("ab") as stream:
            stream.write(b'{"i":1')
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            memory.Store(self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_utf8_boundaries_and_schema_errors(self):
        self.assertEqual(list(memory.chunks("\u6771\u4eac", 3)), ["\u6771", "\u4eac"])
        with memory.Store(self.root) as store:
            with self.assertRaises(ValueError):
                store.append("thought", "must never be logged")
            store.append("user", "first")
            for first, count in [(0, 3), (1, 2), (-1, 1), (True, 1), (0, 8)]:
                with self.assertRaises(ValueError):
                    optchat.zoom(store, first, count)

    def test_batched_view_only_appends_until_high_water(self):
        with patch.multiple(memory, VIEW_LOW=700, VIEW_HIGH=1400, COMPACT_LOW=350, COMPACT_HIGH=700):
            with memory.Store(self.root) as store:
                previous = []
                batch_seen = False
                for i in range(40):
                    store.append("user", "x" * 250 + str(i))
                    # Model summaries for parents make reductions possible.
                    while store.jobs():
                        for key in store.jobs():
                            store.put(*key, "summary " + str(key))
                        store.settle_short()
                    store.update_views()
                    if store.view[:len(previous)] != previous:
                        batch_seen = True
                        self.assertLessEqual(memory.byte_size(store.render()), 700)
                    previous = list(store.view)
                self.assertTrue(batch_seen)

    def test_pending_batch_continues_when_parents_finish(self):
        with patch.multiple(memory, VIEW_LOW=500, VIEW_HIGH=1000, COMPACT_LOW=250, COMPACT_HIGH=500):
            with memory.Store(self.root) as store:
                for i in range(8):
                    store.append("user", "x" * 300 + str(i))
                self.assertTrue(store.state("batch"))
                while store.jobs():
                    for key in store.jobs():
                        store.put(*key, "merged")
                    store.settle_short()
                store.update_views()
                self.assertLessEqual(memory.byte_size(store.render()), 500)
                self.assertFalse(store.state("batch"))

    def test_compaction_context_stops_at_first_unbuilt_message(self):
        with memory.Store(self.root) as store:
            store.append("user", "first")
            store.append("user", "x" * 600)
            store.append("user", "third")
            self.assertIn("0+1|", store.context(3))
            self.assertNotIn("1+1|", store.context(3))
            self.assertNotIn("2+1|", store.context(3))
            self.assertNotIn("placeholder", store.context(3))

    def test_queue_limits_preceding_unbuilt_leaves(self):
        with memory.Store(self.root) as store:
            for i in range(20):
                store.append("user", "x" * 600 + str(i))
            self.assertEqual(store.jobs(), [(0, i) for i in range(8)])
            self.assertFalse(store.jobs(exclude={(0, i) for i in range(8)}))

    def test_background_compaction_finishes_dependencies(self):
        with memory.Store(self.root) as store:
            for i in range(12):
                store.append("user", "x" * 600 + str(i))
        calls = []
        def fake(prompt, model):
            calls.append(prompt)
            self.assertNotIn("(not summarized yet", prompt)
            return "user: compressed"
        count = optchat.compact(self.root, summarizer=fake)
        self.assertEqual(count, 12)
        with memory.Store(self.root) as store:
            self.assertFalse(store.jobs())
            self.assertIsNotNone(store.node(3, 0))

    def test_failed_compaction_remains_queued_for_next_turn(self):
        with memory.Store(self.root) as store:
            store.append("user", "x" * 600)
        def failed(prompt, model):
            raise RuntimeError("simulated provider failure")
        self.assertEqual(optchat.compact(self.root, summarizer=failed), 0)
        with memory.Store(self.root) as store:
            self.assertEqual(store.jobs(), [(0, 0)])
            self.assertIn("simulated", store.state("last_compaction_error"))
        self.assertEqual(optchat.compact(self.root, summarizer=lambda *args: "recovered"), 1)

    def test_rollout_ingestion_is_incremental_and_deduplicates_hooks(self):
        rollout = self.root / "rollout.jsonl"
        rows = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "hello"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": "PRIVATE THOUGHT"}},
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "PRIVATE THOUGHT"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call", "arguments": '{"cmd":"ls"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call", "output": "file.py"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "phase": "final", "message": "done"}},
        ]
        rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
        event = {"session_id": "session", "turn_id": "turn", "transcript_path": str(rollout),
                 "hook_event_name": "Stop", "last_assistant_message": "done"}
        with memory.Store(self.root) as store:
            optchat.ingest(store, event)
            self.assertEqual(store.total, 4)
            optchat.ingest(store, event)
            optchat.ingest(store, dict(event, hook_event_name="UserPromptSubmit", prompt="hello"))
            optchat.ingest(store, dict(event, hook_event_name="PostToolUse", tool_use_id="call",
                                      tool_name="shell", tool_input={"cmd": "ls"}, tool_response="file.py"))
            self.assertEqual(store.total, 4)
            self.assertNotIn("PRIVATE THOUGHT", "".join(p.read_text() for p in self.root.glob("*/*.jsonl")))
            # A user can intentionally repeat the same prompt in a new turn.
            # Only delivery retries of that particular turn are duplicates.
            with rollout.open("a") as stream:
                for row in (
                    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn2"}},
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
                ):
                    stream.write(json.dumps(row) + "\n")
            repeated = dict(event, turn_id="turn2", hook_event_name="UserPromptSubmit", prompt="hello")
            optchat.ingest(store, repeated)
            optchat.ingest(store, repeated)
            self.assertEqual(store.total, 5)
            self.assertEqual(store.message(0)["text"], store.message(4)["text"])

    def test_partial_rollout_record_is_read_only_when_complete(self):
        rollout = self.root / "rollout.jsonl"
        row = json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}})
        rollout.write_text(row)
        event = {"session_id": "session", "transcript_path": str(rollout)}
        with memory.Store(self.root) as store:
            optchat.ingest(store, event)
            self.assertEqual(store.total, 0)
            with rollout.open("a") as stream:
                stream.write("\n")
            optchat.ingest(store, event)
            self.assertEqual(store.total, 1)

    def test_interrupted_reasoning_replacement_preserves_evidence_and_resumes(self):
        rollout = self.root / "damaged.jsonl"
        prefix = "gAAAAA" + "a" * 64
        fragment = {"timestamp": "old", "ordinal": 12, "type": "response_item", "payload": {
            "type": "reasoning", "id": "rs_example", "summary": [], "encrypted_content": prefix}}
        broken = json.dumps(fragment).encode()[:-3] + b"\n"
        replacement = dict(fragment, timestamp="new", payload=dict(fragment["payload"], encrypted_content=prefix + "tail"))
        replacement_bytes = (json.dumps(replacement) + "\n").encode()
        before = (json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "before"}}) + "\n").encode()
        after = (json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "after"}}) + "\n").encode()
        original = before + broken + replacement_bytes + after
        rollout.write_bytes(original)
        event = {"session_id": "s", "turn_id": "t", "transcript_path": str(rollout),
                 "hook_event_name": "UserPromptSubmit", "prompt": "current prompt"}
        with memory.Store(self.root) as store:
            optchat.ingest(store, event)
            self.assertEqual([store.message(i)["text"] for i in range(store.total)],
                             ["before", "after", "current prompt"])
            cursor_key = "transcript:" + str(rollout.resolve())
            self.assertEqual(store.state(cursor_key)["offset"], len(original))
            diagnostic = store.state("last_replay_error")
            self.assertEqual(diagnostic["offset"], len(before))
            self.assertEqual(diagnostic["bytes"], len(broken))
            self.assertEqual(diagnostic["sha256"], optchat.hashlib.sha256(broken).hexdigest())
            self.assertEqual(diagnostic["recovery"], "superseded_reasoning")
            self.assertEqual(diagnostic["replacement"], {
                "offset": len(before) + len(broken), "bytes": len(replacement_bytes),
                "sha256": optchat.hashlib.sha256(replacement_bytes).hexdigest(),
                "ordinal": 12, "reasoning_id": "rs_example"})
            store.set_state(cursor_key, {"offset": len(before), "turn": "t"})
            optchat.ingest(store, event)
            self.assertEqual(store.total, 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM state WHERE key LIKE 'transcript-error:%'").fetchone()[0], 1)
        with memory.Store(self.root) as store:
            self.assertEqual(store.state("last_replay_error"), diagnostic)
        status = optchat.invoke("status", {})
        self.assertEqual(status["replay_errors"], 1)
        self.assertEqual(status["last_replay_error"], diagnostic)
        self.assertEqual(rollout.read_bytes(), original)
        self.assertNotIn(prefix, "".join(p.read_text() for p in self.root.glob("*/*.jsonl")))

    def test_unrecoverable_records_block_replay_but_capture_direct_fields(self):
        broken_records = [
            b'{"type":"response_item","payload":{"type":"reasoning","encrypted_content":"PRIVATE\n',
            b'{"type":"event_msg","payload":{"type":"user_message","message":"a\x01b"}}\n',
            b'\xff\n', b'[]\n',
        ]
        for broken in broken_records:
            with self.subTest(record=broken), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                rollout = root / "damaged.jsonl"
                original = broken + (json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "after"}}) + "\n").encode()
                rollout.write_bytes(original)
                event = {"session_id": "s", "turn_id": "t", "transcript_path": str(rollout),
                         "hook_event_name": "UserPromptSubmit", "prompt": "current prompt"}
                with memory.Store(root) as store:
                    blocked = optchat.ingest(store, event)
                    self.assertEqual(blocked["recovery"], "blocked")
                    self.assertEqual(store.state("transcript:" + str(rollout.resolve()))["offset"], 0)
                    optchat.ingest(store, event)
                    self.assertEqual(store.total, 1)
                    self.assertEqual(store.message(0)["text"], "current prompt")
                with patch.dict(os.environ, {"OPTCHAT_DIR": str(root)}), patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(io.StringIO()) as output:
                    optchat.hook()
                result = json.loads(output.getvalue())
                self.assertIn("hookSpecificOutput", result)
                self.assertIn("replay is blocked", result["systemMessage"])
                self.assertEqual(rollout.read_bytes(), original)

    def test_reasoning_recovery_requires_complete_matching_replacement(self):
        prefix = "gAAAAA" + "a" * 64
        fragment = {"ordinal": 12, "type": "response_item", "payload": {
            "type": "reasoning", "id": "rs_example", "encrypted_content": prefix}}
        broken = json.dumps(fragment).encode()[:-3] + b"\n"
        for mutation in ({"ordinal": 13}, {"payload": dict(fragment["payload"], id="different")},
                         {"payload": dict(fragment["payload"], encrypted_content="different")},
                         {"type": "event_msg"}, {"payload": []}):
            candidate = json.dumps(dict(fragment, **mutation)).encode() + b"\n"
            self.assertIsNone(optchat.superseding_reasoning(io.BytesIO(candidate), broken))
        replacement = json.dumps(dict(fragment, payload=dict(fragment["payload"], encrypted_content=prefix + "tail"))).encode()
        rollout = self.root / "partial.jsonl"
        event = {"session_id": "s", "transcript_path": str(rollout)}
        # An incomplete EOF fragment is never consumed or diagnosed.
        rollout.write_bytes(broken[:-1])
        with memory.Store(self.root) as store:
            optchat.ingest(store, event)
            self.assertEqual(store.state("transcript:" + str(rollout.resolve()))["offset"], 0)
            self.assertIsNone(store.state("last_replay_error"))
        # A newline-terminated fragment must await the complete replacement too.
        rollout.write_bytes(broken + replacement)
        with memory.Store(self.root) as store:
            self.assertIsNotNone(optchat.ingest(store, event))
            self.assertEqual(store.state("transcript:" + str(rollout.resolve()))["offset"], 0)
        with rollout.open("ab") as stream:
            stream.write(b"\n")
        with memory.Store(self.root) as store:
            self.assertIsNone(optchat.ingest(store, event))
            self.assertEqual(store.state("transcript:" + str(rollout.resolve()))["offset"], rollout.stat().st_size)
            self.assertEqual(store.state("last_replay_error")["recovery"], "superseded_reasoning")
            self.assertEqual(store.total, 0)

    def test_reasoning_recovery_budget_boundary_keeps_direct_capture(self):
        prefix = "gAAAAA" + "a" * 64
        row = {"ordinal": 12, "type": "response_item", "payload": {
            "type": "reasoning", "id": "rs_example", "encrypted_content": prefix}}
        broken = json.dumps(row).encode()[:-3] + b"\n"
        replacement = (json.dumps(row) + "\n").encode()
        rollout = self.root / "bounded.jsonl"
        rollout.write_bytes(broken + replacement)
        event = {"session_id": "s", "turn_id": "t", "transcript_path": str(rollout),
                 "hook_event_name": "UserPromptSubmit", "prompt": "current prompt"}
        with memory.Store(self.root) as store:
            optchat.ingest(store, event, deadline=0)
            self.assertEqual(store.state("transcript:" + str(rollout.resolve()))["offset"], len(broken))
            self.assertEqual(store.message(0)["text"], "current prompt")
            optchat.ingest(store, event)
            self.assertEqual(store.total, 1)

    def test_current_rollout_messages_exclude_injected_context(self):
        rollout = self.root / "current.jsonl"
        rows = [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "Keep Tree as default"}],
             "internal_chat_message_metadata_passthrough": {"turn_id": "t", "content_item_kinds": ["user.text"]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "INJECTED ENVIRONMENT"}],
             "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["environments.environment_context"]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final",
             "content": [{"type": "output_text", "text": "Tree restored"}]}},
        ]
        rollout.write_text("".join(json.dumps(r) + "\n" for r in rows))
        event = {"session_id": "s", "turn_id": "t", "transcript_path": str(rollout)}
        with memory.Store(self.root) as store:
            optchat.ingest(store, event)
            optchat.ingest(store, dict(event, hook_event_name="UserPromptSubmit", prompt="Keep Tree as default"))
            optchat.ingest(store, dict(event, hook_event_name="Stop", last_assistant_message="Tree restored"))
            self.assertEqual(store.total, 2)
            self.assertEqual([store.message(i)["text"] for i in range(2)], ["Keep Tree as default", "Tree restored"])

    def test_bounded_rollout_replay_resumes_durable_cursor(self):
        rollout = self.root / "bounded.jsonl"
        rows = [{"type": "event_msg", "payload": {"type": "user_message", "message": str(i)}} for i in range(3)]
        lines = [json.dumps(r) + "\n" for r in rows]
        rollout.write_text("".join(lines))
        event = {"session_id": "s", "transcript_path": str(rollout)}
        with memory.Store(self.root) as store:
            optchat.ingest(store, event, deadline=0)
            self.assertEqual(store.total, 1)
            self.assertEqual(store.state("transcript:" + str(rollout.resolve()))["offset"], len(lines[0]))
        with memory.Store(self.root) as store:
            optchat.ingest(store, event)
            self.assertEqual([store.message(i)["text"] for i in range(3)], ["0", "1", "2"])

    def test_duplicate_append_does_not_rewrite_views(self):
        with memory.Store(self.root) as store:
            store.append("user", "same", source="stable")
            with patch.object(store, "update_views", side_effect=AssertionError("duplicate rebuilt views")):
                self.assertEqual(store.append("user", "same", source="stable"), [0])

    def test_subagent_hook_stores_only_report(self):
        rollout = self.root / "agent.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "private subagent steps"}}) + "\n")
        with memory.Store(self.root) as store:
            optchat.ingest(store, {"session_id": "parent", "turn_id": "turn", "agent_id": "Scout",
                                  "transcript_path": str(rollout), "hook_event_name": "SubagentStop",
                                  "last_assistant_message": "Found the cause"})
            self.assertEqual(store.total, 1)
            self.assertEqual(store.message(0)["kind"], "work")
            self.assertEqual(store.message(0)["text"], "[Scout] Found the cause")

    def test_session_hook_returns_bounded_context_and_stop_does_not_continue(self):
        event = {"session_id": "session", "hook_event_name": "SessionStart"}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(io.StringIO()) as output:
            optchat.hook()
        result = json.loads(output.getvalue())
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertLess(memory.byte_size(result["hookSpecificOutput"]["additionalContext"]), 4000)
        self.assertNotIn("</chat>", result["hookSpecificOutput"]["additionalContext"])
        event.update(hook_event_name="Stop", turn_id="t", last_assistant_message="done")
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(io.StringIO()) as output:
            optchat.hook()
        self.assertEqual(json.loads(output.getvalue()), {})

    def test_user_hook_delivers_routing_without_copying_prompt_or_memory(self):
        event = {"session_id": "s", "turn_id": "t", "hook_event_name": "UserPromptSubmit",
                 "prompt": "Keep this request exactly: 東京!"}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(io.StringIO()) as output:
            optchat.hook()
        result = json.loads(output.getvalue())["hookSpecificOutput"]
        self.assertEqual(result["hookEventName"], "UserPromptSubmit")
        self.assertNotIn(event["prompt"], result["additionalContext"])
        self.assertNotIn("<chat>", result["additionalContext"])
        with memory.Store(self.root) as store:
            self.assertEqual(store.total, 1)
            self.assertEqual(store.message(0)["text"], event["prompt"])

    def test_private_worker_hooks_do_not_dispatch_recursively(self):
        for name in ("SessionStart", "UserPromptSubmit"):
            event = {"session_id": "worker", "turn_id": "t", "agent_id": "child",
                     "hook_event_name": name, "prompt": "OPTCHAT_WORKER_V1 task"}
            with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(io.StringIO()) as output:
                optchat.hook()
            self.assertEqual(json.loads(output.getvalue()), {})
        with memory.Store(self.root) as store:
            self.assertEqual(store.total, 0)

    def test_worker_view_pages_cover_memory_without_oversized_results(self):
        with memory.Store(self.root) as store:
            for i in range(110):
                store.append("user", "%d " % i + "東京" * 75)
            original = store.render()
        page = optchat.invoke("view", {})
        self.assertGreater(page["pages"], 1)
        lines = []
        for number in range(1, page["pages"] + 1):
            result = optchat.invoke("view", {"page": number})
            self.assertLessEqual(memory.byte_size(result["text"]), 20000)
            lines.extend(result["text"].splitlines()[1:-1])
        self.assertEqual("<chat>\n" + "\n".join(lines) + "\n</chat>\n", original)

    def test_mcp_wire_protocol_and_invalid_navigation(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "view", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "zoom", "arguments": {"id": -1}}},
        ]
        result = subprocess.run([sys.executable, str(SCRIPTS / "optchat.py"), "serve"],
                                input="\n".join(json.dumps(r) for r in requests) + "\n",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]}, {"view", "zoom", "date", "status"})
        self.assertIn("<chat>", replies[2]["result"]["structuredContent"]["text"])
        self.assertTrue(replies[3]["result"]["isError"])

    def test_legacy_import_is_idempotent_and_source_unchanged(self):
        path = self.root / "LOG.txt"
        path.write_bytes(b"#0 2026-10-07 old memory".ljust(319, b" ") + b"\n")
        before = path.read_bytes()
        optchat.import_optmem(path)
        optchat.import_optmem(path)
        with memory.Store(self.root) as store:
            self.assertEqual(store.total, 1)
            self.assertEqual(store.message(0)["kind"], "note")
            self.assertEqual(store.message(0)["text"], "old memory")
        self.assertEqual(path.read_bytes(), before)


class AlgorithmTests(unittest.TestCase):
    def test_codex_compactor_configuration_and_tool_rejection(self):
        response = {"type": "item.completed", "item": {"type": "agent_message", "text": "user: keep it"}}
        with patch.object(optchat.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response), "")) as run:
            self.assertEqual(optchat.call_codex("input", "model-name"), "user: keep it")
            args, kwargs = run.call_args
            for setting in ("features.hooks=false", "features.shell_tool=false", "features.multi_agent=false",
                            "agents.enabled=false", "features.apps=false", "features.plugins=false", 'web_search="disabled"'):
                self.assertIn(setting, args[0])
            self.assertEqual(args[0][-3:], ["--model", "model-name", "-"])
            self.assertEqual(kwargs["env"]["OPTCHAT_COMPACTOR"], "1")
            self.assertEqual(kwargs["input"], "input")
        response["item"] = {"type": "mcp_tool_call"}
        with patch.object(optchat.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response), "")):
            with self.assertRaisesRegex(RuntimeError, "tool call"):
                optchat.call_codex("input")

    def test_last_message_age_regression_at_ten_messages(self):
        pairs = [[2, 0], [2, 1], [0, 8], [0, 9]]
        def text(level, index):
            return "x" * (200 - len("%d+%d|\n" % (index << level, 1 << level)))
        result = memory.shrink(pairs, text, 10, 616)
        self.assertEqual(result, [[2, 0], [2, 1], [1, 4]])

    def test_rollback_push_equivalence_for_twenty_thousand_messages(self):
        states, view = [], []
        for tick in range(20001):
            carry = tick
            for state in states:
                if state[0] == 0:
                    state[0] = 1
                    break
                state[0], state[1], carry = 0, carry, state[1]
            else:
                states.append([0, carry])
            total = tick + 1
            view.append([0, tick])
            def text(level, index):
                if (index + 1) << level > total:
                    return None
                return "x" * (200 - len("%d+%d|\n" % (index << level, 1 << level)))
            view = memory.shrink(view, text, total, len(states) * 200 + memory.byte_size("<chat>\n</chat>\n"))
            starts = sorted(s[1] for s in states)
            expected = []
            for a, b in zip(starts, starts[1:] + [total]):
                count = b - a
                level = count.bit_length() - 1
                expected.append([level, a // count])
            self.assertEqual(view, expected, "rollback mismatch at tick %d" % tick)

    def test_oversize_retries_keep_shortest_and_utf8_ruler(self):
        replies = iter(["\u00e9" * 300, "shorter"])
        prompts = []
        def fake(prompt, model):
            prompts.append(prompt)
            return next(replies)
        self.assertEqual(optchat.summarize("task", caller=fake), "shorter")
        self.assertIn("600 bytes", prompts[1])
        self.assertIn("\u00e9" * 256 + "|", prompts[1])
        calls = []
        self.assertEqual(optchat.summarize("task", caller=lambda *args: calls.append(1) or "x" * 520), "x" * 520)
        self.assertEqual(len(calls), 5)


if __name__ == "__main__":
    unittest.main()
