import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "dsh" / "llm-pi-ai" / "index.js"
ENGINEERING_PROTOCOL = (
    ROOT
    / "dsh"
    / "plugins"
    / "dsh-local-qwen-engineering-protocol"
    / "lib"
    / "index.js"
)
COMPACTION_POLICY = ROOT / "config" / "compaction-policy.yaml.example"


class DshLocalBrainRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter_source = ADAPTER.read_text(encoding="utf-8")

    def _adapter_harness(self):
        script = r'''
import fs from "node:fs";

const source = fs.readFileSync("dsh/llm-pi-ai/index.js", "utf8");

function extractFunction(name) {
  const marker = `function ${name}`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

const predicate = extractFunction("isLocalBrainRoute");
const dropper = extractFunction("dropLocalBrainInheritedMaxTokens");
const providerConstant = source.match(/const LOCAL_BRAIN_PROVIDER = [^;]+;/)?.[0];
const modelConstant = source.match(/const LOCAL_BRAIN_MODEL = [^;]+;/)?.[0];
if (!providerConstant || !modelConstant) throw new Error("missing Local Brain route constants");
const helpers = Function(
  `${providerConstant}\n${modelConstant}\n${predicate}\n${dropper}\nreturn { isLocalBrainRoute, dropLocalBrainInheritedMaxTokens };`
)();
const inherited = helpers.dropLocalBrainInheritedMaxTokens({
  max_tokens: 32768,
  max_completion_tokens: 32768,
  marker: "kept"
});
console.log(JSON.stringify({
  exact: helpers.isLocalBrainRoute("local-qwen", "li-huahua-local"),
  deepseek: helpers.isLocalBrainRoute("deepseek", "DeepSeek-V4-Pro"),
  wrongModel: helpers.isLocalBrainRoute("local-qwen", "other-model"),
  inherited,
  oldDropperPresent: source.includes("dropLocalQwenInheritedMaxTokens"),
  explicitGuardPresent: source.includes(
    "isLocalBrainRoute(options.provider, options.model) && model.api === \"openai-completions\" && options.maxTokens === void 0"
  )
}));
'''
        completed = subprocess.run(
            ["node", "--input-type=module", "-"],
            cwd=ROOT,
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _engineering_protocol_harness(self):
        script = r'''
import fs from "node:fs";

const source = fs.readFileSync(
  "dsh/plugins/dsh-local-qwen-engineering-protocol/lib/index.js",
  "utf8"
);
const module = await import(`data:text/javascript,${encodeURIComponent(source)}`);
const local = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "local-qwen", model: "li-huahua-local" }
});
const deepseek = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "deepseek", model: "DeepSeek-V4-Pro" }
});
const wrongModel = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "local-qwen", model: "other-model" }
});
const repeated = module.appendLocalQwenOverlay(local);
console.log(JSON.stringify({
  exact: module.isLocalBrainRoute("local-qwen", "li-huahua-local"),
  localSections: local.sections.length,
  localSectionName: local.sections[0]?.name,
  protocolHeading: local.sections[0]?.text.startsWith("# Local Qwen Engineering Protocol"),
  deepseekSections: deepseek.sections.length,
  wrongModelSections: wrongModel.sections.length,
  repeatedSections: repeated.sections.length,
  oldPredicatePresent: source.includes("isLocalQwenRoute")
}));
'''
        completed = subprocess.run(
            ["node", "--input-type=module", "-"],
            cwd=ROOT,
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def test_inherited_max_tokens_are_dropped(self):
        result = self._adapter_harness()
        self.assertEqual(result["inherited"], {"marker": "kept"})
        self.assertFalse(result["oldDropperPresent"])

    def test_explicit_max_tokens_are_preserved_by_adapter_guard(self):
        result = self._adapter_harness()
        self.assertTrue(result["explicitGuardPresent"])
        self.assertIn(
            '...options.maxTokens === void 0 ? {} : { maxTokens: options.maxTokens },',
            self.adapter_source,
        )

    def test_non_local_provider_is_unchanged(self):
        result = self._adapter_harness()
        self.assertFalse(result["deepseek"])
        self.assertFalse(result["wrongModel"])
        self.assertNotIn('options.provider === "local-qwen"', self.adapter_source)

    def test_engineering_protocol_matches_exact_route(self):
        result = self._engineering_protocol_harness()
        self.assertTrue(result["exact"])
        self.assertEqual(result["localSections"], 1)
        self.assertEqual(result["localSectionName"], "local-qwen:engineering-protocol")
        self.assertTrue(result["protocolHeading"])

    def test_engineering_protocol_rejects_nonmatching_routes(self):
        result = self._engineering_protocol_harness()
        self.assertEqual(result["deepseekSections"], 0)
        self.assertEqual(result["wrongModelSections"], 0)
        self.assertFalse(result["oldPredicatePresent"])

    def test_engineering_protocol_remains_idempotent_and_compaction_values_exact(self):
        result = self._engineering_protocol_harness()
        self.assertEqual(result["repeatedSections"], 1)
        policy = COMPACTION_POLICY.read_text(encoding="utf-8")
        expected = (
            "provider: local-qwen\n"
            "        model: li-huahua-local\n"
            "        thresholdRatio: 0.68\n"
            "        retainRatio: 0.15\n"
            "        maxTokens: 8192"
        )
        self.assertIn(expected, policy)


if __name__ == "__main__":
    unittest.main()
