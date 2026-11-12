"""Unit and contract tests for roles/observability_llm/files/mlx_telemetry.py.

Standard library only, Python 3.9+ (the agent's own constraint):

    python3 -m unittest discover -s tests -v

A fake MLX-LM server, built from mlx_lm 0.31 server.py's response format
(json.dumps defaults, logprobs only on non-streaming replies, complete tool
calls per streamed chunk), stands in for the real one.
"""

import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "..", "roles", "observability_llm", "files", "mlx_telemetry.py")
spec = importlib.util.spec_from_file_location("mlx_telemetry", AGENT)
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)


def mlx_reply(body):
    """What mlx_lm.server sends for a non-streaming chat request."""
    choice = {"index": 0, "finish_reason": body.get("_finish", "stop")}
    if body.get("logprobs"):
        choice["logprobs"] = {"content": [{"id": 1, "logprob": -0.01}, {"id": 2, "logprob": -2.3},
                                          {"id": 3, "logprob": -0.1}]}
    message = {"role": "assistant", "content": "Hello there"}
    if body.get("tools"):
        message["tool_calls"] = [{"function": {"name": "get_weather", "arguments": "{}"}, "type": "function", "id": "a"}]
        choice["finish_reason"] = "tool_calls"
    if body.get("_think"):
        message["reasoning"] = "thinking..."
    choice["message"] = message
    return {"id": "x", "system_fingerprint": "f", "object": "chat.completion", "model": "m", "created": 1,
            "choices": [choice], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
                                           "prompt_tokens_details": {"cached_tokens": 2}}}


class FakeMLX(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BaseHTTP/0.6"
    seen = []

    def log_message(self, *args):
        return

    def do_GET(self):
        data = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeMLX.seen.append(body)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "content": "Hi"}}]},
                      {"choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "tool_calls": [
                          {"function": {"name": "search", "arguments": "{}"}, "type": "function", "id": "b", "index": 0}]}}]},
                      {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {"role": "assistant"}}]},
                      {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 6}}]
            for chunk in chunks:
                self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True
            return
        data = json.dumps(mlx_reply(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def series(metric):
    """{frozenset(labels): value} of a counter or gauge."""
    return {frozenset(k): v for k, v in metric._values.items()}


def labelled(metric, **want):
    return sum(v for k, v in metric._values.items() if all(dict(k).get(a) == b for a, b in want.items()))


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeMLX)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        FakeMLX.seen = []

    def tearDown(self):
        self.upstream.shutdown()
        if getattr(self, "proxy", None):
            self.proxy.shutdown()
            self.proxy.server_close()
        self.upstream.server_close()

    def start(self, **overrides):
        cfg = json.loads(json.dumps(mt.DEFAULTS))
        cfg.update(upstream_port=self.upstream.server_address[1], proxy_port=0, **overrides)
        self.cfg, self.metrics, self.state = cfg, mt.Metrics(cfg), mt.State(cfg)
        self.state.framework = "mlx-lm"
        self.proxy = mt.run_proxy(cfg, self.metrics, self.state)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.proxy.server_address[1]

    def post(self, base, body):
        req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    def wait_recorded(self, n=1):
        for _ in range(100):
            if sum(self.metrics.requests._values.values()) >= n:
                return
            threading.Event().wait(0.02)
        self.fail("request not recorded")

    def test_injected_logprobs_are_scored_and_stripped_byte_identically(self):
        base = self.start(confidence_metrics_enabled=True, confidence_inject_logprobs=True)
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 3}
        got = self.post(base, body)
        self.wait_recorded()
        self.assertTrue(FakeMLX.seen[0]["logprobs"])
        # Exactly what MLX-LM sends when logprobs were not asked for.
        self.assertEqual(got, json.dumps(mlx_reply(body)).encode())
        self.assertEqual(labelled(self.metrics.c_scored, source="injected"), 1)
        self.assertEqual(sum(self.metrics.c_low._values.values()), 1)  # 1 of 3 tokens < 0.5

    def test_client_logprobs_are_kept_and_scored(self):
        base = self.start(confidence_metrics_enabled=True, confidence_inject_logprobs=True)
        got = json.loads(self.post(base, {"messages": [], "logprobs": True}))
        self.wait_recorded()
        self.assertIn("logprobs", got["choices"][0])
        self.assertEqual(labelled(self.metrics.c_scored, source="client"), 1)

    def test_nothing_injected_by_default(self):
        base = self.start()
        self.post(base, {"messages": []})
        self.wait_recorded()
        self.assertNotIn("logprobs", FakeMLX.seen[0])
        self.assertEqual(self.metrics.c_scored._values, {})

    def test_finish_reason_kind_and_tool_names(self):
        base = self.start()
        self.post(base, {"messages": [], "tools": [{"type": "function"}]})
        self.post(base, {"messages": [], "_think": True})
        self.post(base, {"messages": [], "_finish": "length"})
        self.wait_recorded(3)
        r = self.metrics.responses
        self.assertEqual(labelled(r, kind="tool_call", finish_reason="tool_calls"), 1)
        self.assertEqual(labelled(r, kind="reasoning", finish_reason="stop"), 1)
        self.assertEqual(labelled(r, kind="chat", finish_reason="length"), 1)
        self.assertEqual(labelled(self.metrics.tool_calls_by_tool, tool="get_weather"), 1)

    def test_streaming_tool_calls_and_finish_reason(self):
        base = self.start()
        self.post(base, {"messages": [], "stream": True})
        self.wait_recorded()
        self.assertEqual(labelled(self.metrics.tool_calls_by_tool, tool="search"), 1)
        self.assertEqual(labelled(self.metrics.responses, kind="tool_call", finish_reason="tool_calls", stream="true"), 1)


class HelpersTest(unittest.TestCase):
    def test_tool_label_is_sanitised_and_capped(self):
        cfg = json.loads(json.dumps(mt.DEFAULTS))
        cfg["tool_label_max_values"] = 2
        state = mt.State(cfg)
        self.assertEqual(state.tool_label("get weather!"), "get_weather_")
        self.assertEqual(state.tool_label("b"), "b")
        self.assertEqual(state.tool_label("c"), "other")
        self.assertEqual(state.tool_label("b"), "b")
        self.assertEqual(state.tool_label(None), "other")

    def test_streamed_fragments_without_name_are_not_counted(self):
        result = {"tool_calls": 0, "tool_names": []}
        mt.ProxyHandler._collect_tools(result, [{"index": 0, "function": {"name": "f", "arguments": ""}, "id": "1"}], True)
        mt.ProxyHandler._collect_tools(result, [{"index": 0, "function": {"arguments": "{\"a\": 1}"}}], True)
        self.assertEqual((result["tool_calls"], result["tool_names"]), (1, ["f"]))

    def test_finish_label_is_bounded(self):
        self.assertEqual(mt._finish_label(None), "none")
        self.assertEqual(mt._finish_label("length"), "length")
        self.assertEqual(mt._finish_label("weird-new-reason"), "other")

    def test_mlx_lm_top_logprobs_are_not_scored(self):
        lp = {"content": [{"id": 1, "logprob": -0.1, "top_logprobs": [{"id": 1, "logprob": -0.1}]}]}
        self.assertIsNone(mt._sampled_logprobs(lp, "mlx-lm"))
        self.assertEqual(mt._sampled_logprobs(lp, "mlx-vlm"), [-0.1])
        self.assertEqual(mt._sampled_logprobs({"token_logprobs": [-0.5, None]}, "mlx-lm"), [-0.5])

    def test_score_confidence(self):
        cfg = mt.DEFAULTS
        mean, low, ppl, ratio = mt.score_confidence([0.0, 0.0], cfg)
        self.assertEqual((mean, low, ppl, ratio), (1.0, 1.0, 1.0, 0.0))
        mean, low, ppl, ratio = mt.score_confidence([-0.01, -2.3], cfg)
        self.assertAlmostEqual(ratio, 0.5)
        self.assertGreater(ppl, 3)

    def test_architecture_from_qwen3_config(self):
        cfg = {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "head_dim": 128,
               "hidden_size": 2560, "num_attention_heads": 32, "num_hidden_layers": 36,
               "num_key_value_heads": 8, "vocab_size": 151936, "intermediate_size": 9728,
               "quantization": {"group_size": 64, "bits": 4}}
        attrs, labels, kv, weights = mt._architecture_from(cfg)
        self.assertEqual(attrs["layers"], 36)
        self.assertEqual(attrs["quant_bits"], 4)
        self.assertEqual(labels, {"architecture": "Qwen3ForCausalLM", "model_type": "qwen3", "quantization": "4-bit"})
        self.assertEqual(kv, 2 * 36 * 8 * 128 * 2)  # 147456 bytes/token
        self.assertIsNone(weights)

    def test_architecture_from_vlm_text_config_and_derived_head_dim(self):
        cfg = {"text_config": {"num_hidden_layers": 2, "num_attention_heads": 4, "hidden_size": 64}}
        attrs, _labels, kv, _w = mt._architecture_from(cfg)
        self.assertEqual((attrs["kv_heads"], attrs["head_dim"]), (4, 16))
        self.assertEqual(kv, 2 * 2 * 4 * 16 * 2)

    def test_size_from_argv(self):
        cmd = "python -m mlx_lm.server --model m --prompt-cache-size 4 --prompt-cache-bytes 8GB"
        self.assertEqual(mt._size_from_argv(cmd, "--prompt-cache-size"), 4)
        self.assertEqual(mt._size_from_argv(cmd, "--prompt-cache-bytes"), 8 * 1024 ** 3)
        self.assertIsNone(mt._size_from_argv("python -m mlx_lm.server", "--prompt-cache-bytes"))

    def test_config_validation(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"confidence_low_token_probability": 2, "tool_label_max_values": 0}, handle)
        try:
            with self.assertRaises(ValueError) as ctx:
                mt.load_config(handle.name)
            self.assertIn("confidence_low_token_probability", str(ctx.exception))
            self.assertIn("tool_label_max_values", str(ctx.exception))
        finally:
            os.unlink(handle.name)


class LogCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "server.log")
        cfg = json.loads(json.dumps(mt.DEFAULTS))
        cfg["server_log_path"] = self.log
        self.m = mt.Metrics(cfg)
        self.c = mt.MLXLMLogCollector(cfg, self.m)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, mode="a"):
        with open(self.log, mode) as handle:
            handle.write(text)

    def test_reads_latest_prompt_cache_line(self):
        self.write("2026-11-11 10:00:00,000 - INFO - Prompt Cache: 1 sequences, 0.05 GB\n"
                   "2026-11-11 10:00:00,000 - INFO - - KVCache: 1 sequences, 0.05 GB\n")
        cmd = "python -m mlx_lm.server --prompt-cache-size 4 --prompt-cache-bytes 1G"
        self.c.poll(True, "qwen", cmd)
        self.assertEqual(series(self.m.kv_used), {frozenset({("source", "mlx-lm"), ("tier", "hot"), ("model", "qwen")}): 5e7})
        self.assertEqual(labelled(self.m.kv_entries_capacity, source="mlx-lm"), 4)
        self.assertEqual(labelled(self.m.kv_capacity, source="mlx-lm"), 1024 ** 3)
        self.assertEqual(labelled(self.m.kv_up, source="mlx-lm"), 1)
        self.write("... - INFO - Prompt Cache: 3 sequences, 0.20 GB\npartial line without newl")
        self.c.poll(True, "qwen", "")
        self.assertEqual(labelled(self.m.kv_entries, source="mlx-lm"), 3)
        self.assertEqual(labelled(self.m.kv_entries_capacity, source="mlx-lm"), 10)  # MLX-LM default

    def test_truncation_and_missing_file(self):
        self.c.poll(True, "m", "")
        self.assertEqual(labelled(self.m.kv_failures, source="mlx-lm", reason="connect"), 1)
        self.write("x - Prompt Cache: 2 sequences, 1.00 GB\n")
        self.c.poll(True, "m", "")
        self.write("y - Prompt Cache: 1 sequences, 0.10 GB\n", mode="w")  # rotated / truncated
        self.c.poll(True, "m", "")
        self.assertEqual(labelled(self.m.kv_used, source="mlx-lm"), 1e8)

    def test_no_line_yet_reports_up_without_figures(self):
        self.write("2026 - INFO - Starting httpd\n")
        self.c.poll(True, "m", "")
        self.assertEqual(labelled(self.m.kv_up, source="mlx-lm"), 1)
        self.assertEqual(self.m.kv_used._values, {})

    def test_not_mlx_lm_drops_series(self):
        self.write("Prompt Cache: 1 sequences, 0.01 GB\n")
        self.c.poll(True, "m", "")
        self.c.poll(False, "m", "")
        self.assertEqual(self.m.kv_used._values, {})
        self.assertEqual(self.m.kv_up._values, {})


if __name__ == "__main__":
    unittest.main()
