"""Tests for the viewer page's JavaScript multipart parser (runs under node)."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

import camd  # noqa: F401  (ensures the package root is importable)

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

HARNESS = r"""
const assert = require("assert");
const P = globalThis.MJPEGParser;
assert.ok(P, "parser exported");

function part(f) {
  const head = `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${f.length}\r\n\r\n`;
  const bytes = new Uint8Array(head.length + f.length + 2);
  bytes.set(Buffer.from(head), 0);
  bytes.set(f, head.length);
  bytes.set(Buffer.from("\r\n"), head.length + f.length);
  return bytes;
}

const f0 = Uint8Array.from([0xff, 0xd8, 0x01, 0xff, 0xd9]);
const f1 = Uint8Array.from([0xff, 0xd8, 0x02, 0x03, 0xff, 0xd9]);
const stream = Buffer.concat([part(f0), part(f1)]);

// feed one byte at a time to prove incremental parsing at any split point
let pending = new Uint8Array(0);
const got = [];
for (const b of stream) {
  pending = P.append(pending, Uint8Array.of(b));
  for (;;) {
    const r = P.extractPart(pending);
    if (!r) break;
    got.push(Buffer.from(r.frame).toString("hex"));
    pending = pending.subarray(r.consumed);
  }
}
assert.deepStrictEqual(got, [Buffer.from(f0).toString("hex"), Buffer.from(f1).toString("hex")]);

// empty buffer returns null
assert.strictEqual(P.extractPart(new Uint8Array(0)), null);

// truncated tail: the first part still parses, the cut one does not
const cut = stream.subarray(0, stream.length - 1);
const first = P.extractPart(cut);
assert.ok(first, "first part parses");
assert.deepStrictEqual(
  Buffer.from(first.frame).toString("hex"),
  Buffer.from(f0).toString("hex")
);
assert.strictEqual(P.extractPart(cut.subarray(first.consumed)), null);

// part without content-length is rejected
assert.throws(() => P.extractPart(Buffer.from("--frame\r\nContent-Type: image/jpeg\r\n\r\nxx")));

// findBytes sanity
assert.strictEqual(P.findBytes(Buffer.from("abcde"), Buffer.from("cd"), 0), 2);
assert.strictEqual(P.findBytes(Buffer.from("abcde"), Buffer.from("xyz"), 0), -1);

console.log("VIEWER-JS OK");
"""


class ViewerJSParserTest(unittest.TestCase):
    def test_parser(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with open(INDEX_PATH, encoding="utf-8") as fh:
            html = fh.read()
        match = re.search(r"<script>(.*)</script>", html, re.S)
        self.assertIsNotNone(match, "viewer script not found in index.html")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "harness.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(match.group(1) + HARNESS)
            result = subprocess.run(
                [node, path], capture_output=True, text=True, timeout=30
            )
        self.assertEqual(
            result.returncode, 0, "node harness failed:\n" + result.stderr
        )
        self.assertIn("VIEWER-JS OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
