import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from fluctour.codex_transport import DISABLED_FEATURES, FINALIZE_INSTRUCTIONS, app_server_command, run_codex


FAKE_SERVER = r'''
import json,sys,time
mode,logfile=sys.argv[1:]
def send(value):
    print(json.dumps(value),flush=True)
def note(method,params):
    send({'method':method,'params':params})
def usage(total,thread='thread-one'):
    note('thread/tokenUsage/updated',{'threadId':thread,'turnId':'turn-one','tokenUsage':{'total':{'inputTokens':total-10,'outputTokens':10,'cachedInputTokens':5,'reasoningOutputTokens':3,'totalTokens':total},'last':{},'modelContextWindow':100000}})
def finish(status):
    note('turn/completed',{'threadId':'thread-one','turn':{'id':'turn-one','status':status}})
for line in sys.stdin:
    message=json.loads(line)
    with open(logfile,'a') as log:
        log.write(json.dumps(message)+'\n')
    method=message.get('method')
    if method=='initialize':
        send({'id':message['id'],'result':{'userAgent':'test'}})
    elif method=='config/read':
        config={'features':dict.fromkeys(DISABLED,False),'mcp_servers':{},'model_providers':{},'model_provider':'openai','web_search':'live','project_doc_max_bytes':0}
        if mode=='unsafe_configuration':
            config['mcp_servers']={'private_server':{'enabled':True,'token':'SECRET_MCP_TOKEN'}}
        send({'id':message['id'],'result':{'config':config}})
    elif method=='thread/start':
        send({'id':message['id'],'result':{'thread':{'id':'thread-one'},'approvalPolicy':'never','sandbox':{'type':'dangerFullAccess' if mode=='unsafe_thread' else 'readOnly'},'instructionSources':[]}})
    elif method=='turn/start':
        send({'id':message['id'],'result':{'turn':{'id':'turn-one','status':'inProgress'}}})
        note('turn/started',{'threadId':'thread-one','turn':{'id':'turn-one','status':'inProgress'}})
        if mode in ('timeout','hang'):
            continue
        if mode=='rpc_error':
            send({'id':4,'error':{'code':1,'message':'SECRET_PROVIDER_ERROR'}})
            continue
        if mode=='unexpected_request':
            send({'id':999,'method':'item/commandExecution/requestApproval','params':{'command':'SECRET_DANGEROUS_COMMAND'}})
            continue
        if mode!='missing_usage':
            usage(30)
        if mode=='invalid_usage':
            note('thread/tokenUsage/updated',{'threadId':'thread-one','turnId':'turn-one','tokenUsage':{'total':{'inputTokens':-1}}})
            continue
        if mode in ('finalize','unsupported_finalize'):
            usage(35)
            usage(40)
            continue
        if mode in ('budget','callback_stop'):
            if mode=='budget':
                usage(100)
            continue
        if mode=='wrong_thread':
            usage(100000,'another-thread')
        if mode!='missing_usage':
            usage(30)
            usage(55)
        note('item/completed',{'threadId':'thread-one','turnId':'turn-one','item':{'id':'answer','type':'agentMessage','phase':'final_answer','text':'{"summary":"Saved public research"}'}})
        finish('completed')
    elif method=='turn/steer':
        if mode=='unsupported_finalize':
            send({'id':message['id'],'error':{'code':-32601,'message':'Method not found'}})
        else:
            send({'id':message['id'],'result':{'turnId':'turn-one'}})
        usage(65)
    elif method=='turn/interrupt':
        if mode=='hang':
            time.sleep(60)
            continue
        send({'id':message['id'],'result':{}})
        if mode=='budget':
            usage(120)
        finish('interrupted')
'''


class CodexTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / "fake_server.py"
        self.server.write_text("DISABLED=" + repr(DISABLED_FEATURES) + "\n" + FAKE_SERVER)
        self.requests_file = self.root / "requests.jsonl"
        self.processes = []

    def run_fake(self, mode="complete", *, limit=1000, timeout=2, on_usage=None,
                 progress=None, cancel_event=None, finalize_threshold=None):
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process
        command = [sys.executable, "-u", str(self.server), mode, str(self.requests_file)]
        with patch("fluctour.codex_transport.app_server_command", return_value=command), patch(
                "fluctour.codex_transport.subprocess.Popen", side_effect=launch):
            return run_codex("Research public trails.", {"type": "object"}, self.root / "workspace",
                             {"token_limit": limit, "timeout_seconds": timeout, "interrupt_grace_seconds": 0.1,
                              "finalize_token_threshold": finalize_threshold},
                             progress=progress, on_usage=on_usage, cancel_event=cancel_event)

    def requests(self):
        return [json.loads(line) for line in self.requests_file.read_text().splitlines()]

    def tearDown(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)

    def test_command_disables_external_capabilities_and_overrides_then_verifies_config(self):
        with patch("fluctour.codex_transport.shutil.which", return_value="/usr/bin/codex"):
            command = app_server_command()
        self.assertEqual(command[:4], ["/usr/bin/codex", "app-server", "--listen", "stdio://"])
        for name in ("shell_tool", "unified_exec", "multi_agent", "apps", "browser_use", "computer_use", "hooks", "plugins"):
            self.assertTrue(any(command[i:i+2] == ["--disable", name] for i in range(len(command)-1)))
        self.assertIn("mcp_servers={}", command)
        self.assertIn('model_provider="openai"', command)
        self.assertIn("project_doc_max_bytes=0", command)
        self.assertNotIn("--ignore-user-config", command)

    def test_fresh_thread_schema_and_cumulative_usage_are_preserved(self):
        observed, events = [], []
        result = self.run_fake(on_usage=observed.append, progress=events.append)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], {"summary": "Saved public research"})
        self.assertTrue(result["usage_complete"])
        self.assertEqual(result["usage"]["input_tokens"], 45)
        self.assertEqual(result["usage"]["output_tokens"], 10)
        self.assertEqual(result["usage"]["cached_input_tokens"], 5)
        self.assertEqual([item["total_tokens"] for item in observed], [30, 30, 55])
        requests = self.requests()
        self.assertEqual([item["method"] for item in requests], ["initialize", "initialized", "config/read", "thread/start", "turn/start"])
        thread = next(item["params"] for item in requests if item["method"] == "thread/start")
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(thread["approvalPolicy"], "never")
        self.assertEqual(thread["sandbox"], "read-only")
        turn = requests[-1]["params"]
        self.assertEqual(turn["outputSchema"], {"type": "object"})
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(result["thread_id"], "thread-one")
        self.assertEqual(result["turn_id"], "turn-one")
        self.assertFalse(any("result" in event for event in events))

    def test_live_budget_interrupts_and_retains_later_observed_lower_bound(self):
        result = self.run_fake("budget", limit=40)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["stop_reason"], "token_limit")
        self.assertFalse(result["usage_complete"])
        self.assertEqual(result["usage"]["total_tokens"], 120)
        interrupts = [item for item in self.requests() if item["method"] == "turn/interrupt"]
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(interrupts[0]["params"], {"threadId": "thread-one", "turnId": "turn-one"})

    def test_caller_can_stop_at_shared_budget_threshold(self):
        result = self.run_fake("callback_stop", on_usage=lambda usage: False)
        self.assertEqual(result["stop_reason"], "token_limit")
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertFalse(result["usage_complete"])

    def test_early_finalization_steers_once_and_usage_cap_still_interrupts(self):
        for mode, supported in (("finalize", True), ("unsupported_finalize", False)):
            with self.subTest(mode=mode):
                self.requests_file.unlink(missing_ok=True)
                result = self.run_fake(mode, limit=60, finalize_threshold=20)
                self.assertEqual(result["stop_reason"], "token_limit")
                self.assertEqual(result["status"], "interrupted")
                self.assertFalse(result["usage_complete"])
                self.assertTrue(result["finalize_requested"])
                self.assertIs(result["finalize_supported"], supported)
                requests = self.requests()
                steers = [message for message in requests if message.get("method") == "turn/steer"]
                self.assertEqual(len(steers), 1)
                self.assertEqual(steers[0]["params"], {"threadId": "thread-one", "expectedTurnId": "turn-one",
                                                       "input": [{"type": "text", "text": FINALIZE_INSTRUCTIONS}]})
                self.assertEqual(sum(message.get("method") == "turn/interrupt" for message in requests), 1)

    def test_deadline_interrupts_and_unresponsive_process_is_reaped(self):
        for mode in ("timeout", "hang"):
            with self.subTest(mode=mode):
                self.requests_file.unlink(missing_ok=True)
                began = time.monotonic()
                result = self.run_fake(mode, timeout=0.15)
                self.assertLess(time.monotonic() - began, 3)
                self.assertEqual(result["stop_reason"], "timeout")
                self.assertEqual(result["status"], "interrupted")
                self.assertFalse(result["usage_complete"])
                self.assertEqual(result["usage"], {})
                self.assertTrue(any(item["method"] == "turn/interrupt" for item in self.requests()))

    def test_cancellation_before_launch_and_during_turn(self):
        cancel = Event()
        cancel.set()
        with patch("fluctour.codex_transport.app_server_command", return_value=["unused"]), patch(
                "fluctour.codex_transport.subprocess.Popen") as launch:
            result = run_codex("Public request", {}, self.root, {"token_limit": 100}, cancel_event=cancel)
        launch.assert_not_called()
        self.assertEqual(result["stop_reason"], "cancelled")
        cancel.clear()
        def on_usage(usage):
            cancel.set()
        result = self.run_fake("callback_stop", on_usage=on_usage, cancel_event=cancel)
        self.assertEqual(result["stop_reason"], "cancelled")
        self.assertEqual(result["status"], "interrupted")

    def test_missing_or_invalid_usage_remains_unknown(self):
        result = self.run_fake("missing_usage")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"], {})
        self.assertFalse(result["usage_complete"])
        self.requests_file.unlink()
        result = self.run_fake("invalid_usage")
        self.assertEqual(result["stop_reason"], "invalid_usage")
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertFalse(result["usage_complete"])

    def test_other_thread_usage_cannot_affect_this_turn(self):
        result = self.run_fake("wrong_thread", limit=100)
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["stop_reason"])
        self.assertEqual(result["usage"]["total_tokens"], 55)

    def test_unsafe_config_or_effective_thread_permissions_fail_before_inference(self):
        for mode in ("unsafe_configuration", "unsafe_thread"):
            with self.subTest(mode=mode):
                self.requests_file.unlink(missing_ok=True)
                events = []
                result = self.run_fake(mode, progress=events.append)
                self.assertEqual(result["stop_reason"], "unsafe_configuration")
                self.assertFalse(result["usage_complete"])
                self.assertFalse(any(item["method"] == "turn/start" for item in self.requests()))
                self.assertNotIn("SECRET_MCP_TOKEN", json.dumps(events) + json.dumps(result))

    def test_server_actions_are_denied_and_provider_errors_are_sanitized(self):
        events = []
        result = self.run_fake("unexpected_request", progress=events.append)
        self.assertEqual(result["stop_reason"], "unexpected_server_request")
        denial = next(item for item in self.requests() if item.get("id") == 999)
        self.assertEqual(denial["error"]["code"], -32601)
        self.assertNotIn("SECRET_DANGEROUS_COMMAND", json.dumps(events) + json.dumps(result))
        self.requests_file.unlink()
        result = self.run_fake("rpc_error", progress=events.append)
        self.assertEqual(result["stop_reason"], "rpc_error")
        self.assertNotIn("SECRET_PROVIDER_ERROR", json.dumps(events) + json.dumps(result))

    def test_invalid_limits_reject_without_starting_process(self):
        for limits in ({}, {"token_limit": True}, {"token_limit": 0},
                       {"token_limit": 10, "timeout_seconds": float("nan")},
                       {"token_limit": 10, "interrupt_grace_seconds": -1},
                       {"token_limit": 10, "finalize_token_threshold": True},
                       {"token_limit": 10, "finalize_token_threshold": 10}):
            with self.subTest(limits=limits), patch("fluctour.codex_transport.subprocess.Popen") as process:
                with self.assertRaises(ValueError):
                    run_codex("Public request", {}, self.root, limits)
                process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
