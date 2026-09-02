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
SETTINGS_EXAMPLE = ROOT / "config" / "dsh-settings.yaml.example"
COMPACTION_POLICY = ROOT / "config" / "compaction-policy.yaml.example"


class DshLocalBrainRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter_source = ADAPTER.read_text(encoding="utf-8")
        cls.protocol_source = ENGINEERING_PROTOCOL.read_text(encoding="utf-8")

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
const formalProviderConstant = source.match(/const FORMAL_LOCAL_BRAIN_PROVIDER = [^;]+;/)?.[0];
const formalModelConstant = source.match(/const FORMAL_LOCAL_BRAIN_MODEL = [^;]+;/)?.[0];
if (!providerConstant || !modelConstant || !formalProviderConstant || !formalModelConstant) {
  throw new Error("missing Local Brain route constants");
}
const helpers = Function(
  `${providerConstant}\n${modelConstant}\n${formalProviderConstant}\n${formalModelConstant}\n${predicate}\n${dropper}\nreturn { isLocalBrainRoute, dropLocalBrainInheritedMaxTokens };`
)();
const inherited = {
  max_tokens: 32768,
  max_completion_tokens: 32768,
  marker: "kept"
};
console.log(JSON.stringify({
  formal: helpers.isLocalBrainRoute("local-brain", "local-brain-v1"),
  legacy: helpers.isLocalBrainRoute("local-qwen", "li-huahua-local"),
  deepseek: helpers.isLocalBrainRoute("deepseek", "DeepSeek-V4-Pro"),
  wrongModel: helpers.isLocalBrainRoute("local-brain", "other-model"),
  formalInherited: helpers.dropLocalBrainInheritedMaxTokens({ ...inherited }),
  legacyInherited: helpers.dropLocalBrainInheritedMaxTokens({ ...inherited }),
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
const formal = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "local-brain", model: "local-brain-v1" }
});
const legacy = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "local-qwen", model: "li-huahua-local" }
});
const deepseek = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "deepseek", model: "DeepSeek-V4-Pro" }
});
const wrongModel = module.appendLocalQwenOverlay({
  sections: [],
  variables: { provider: "local-brain", model: "other-model" }
});
const repeated = module.appendLocalQwenOverlay(formal);
console.log(JSON.stringify({
  formalExact: module.isLocalBrainRoute("local-brain", "local-brain-v1"),
  legacyExact: module.isLocalBrainRoute("local-qwen", "li-huahua-local"),
  formalSections: formal.sections.length,
  legacySections: legacy.sections.length,
  formalSectionName: formal.sections[0]?.name,
  legacySectionName: legacy.sections[0]?.name,
  protocolHeading: formal.sections[0]?.text.startsWith("# Local Qwen Engineering Protocol"),
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

    def test_formal_route_matches_public_identity(self):
        result = self._adapter_harness()
        self.assertTrue(result["formal"])
        self.assertIn('const FORMAL_LOCAL_BRAIN_PROVIDER = "local-brain";', self.adapter_source)
        self.assertIn('const FORMAL_LOCAL_BRAIN_MODEL = "local-brain-v1";', self.adapter_source)

    def test_legacy_route_remains_available(self):
        result = self._adapter_harness()
        self.assertTrue(result["legacy"])
        self.assertIn('const LOCAL_BRAIN_PROVIDER = "local-qwen";', self.adapter_source)
        self.assertIn('const LOCAL_BRAIN_MODEL = "li-huahua-local";', self.adapter_source)

    def test_inherited_max_tokens_are_dropped_for_both_local_routes(self):
        result = self._adapter_harness()
        self.assertEqual(result["formalInherited"], {"marker": "kept"})
        self.assertEqual(result["legacyInherited"], {"marker": "kept"})
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

    def test_engineering_protocol_matches_formal_route(self):
        result = self._engineering_protocol_harness()
        self.assertTrue(result["formalExact"])
        self.assertEqual(result["formalSections"], 1)
        self.assertEqual(result["formalSectionName"], "local-qwen:engineering-protocol")
        self.assertTrue(result["protocolHeading"])

    def test_engineering_protocol_matches_legacy_route(self):
        result = self._engineering_protocol_harness()
        self.assertTrue(result["legacyExact"])
        self.assertEqual(result["legacySections"], 1)
        self.assertEqual(result["legacySectionName"], "local-qwen:engineering-protocol")

    def test_engineering_protocol_rejects_deepseek_and_wrong_routes(self):
        result = self._engineering_protocol_harness()
        self.assertEqual(result["deepseekSections"], 0)
        self.assertEqual(result["wrongModelSections"], 0)
        self.assertFalse(result["oldPredicatePresent"])

    def test_engineering_protocol_remains_idempotent(self):
        result = self._engineering_protocol_harness()
        self.assertEqual(result["repeatedSections"], 1)
        self.assertIn("This protocol supplements them and does not replace them.", self.protocol_source)

    def test_compaction_formal_policy_values_are_exact(self):
        policy = COMPACTION_POLICY.read_text(encoding="utf-8")
        expected = (
            "provider: local-brain\n"
            "        model: local-brain-v1\n"
            "        thresholdRatio: 0.68\n"
            "        retainRatio: 0.15\n"
            "        maxTokens: 8192"
        )
        self.assertIn(expected, policy)

    def test_compaction_legacy_policy_values_are_exact(self):
        policy = COMPACTION_POLICY.read_text(encoding="utf-8")
        expected = (
            "provider: local-qwen\n"
            "        model: li-huahua-local\n"
            "        thresholdRatio: 0.68\n"
            "        retainRatio: 0.15\n"
            "        maxTokens: 8192"
        )
        self.assertIn(expected, policy)

    def test_compaction_values_are_identical_between_routes(self):
        policy = COMPACTION_POLICY.read_text(encoding="utf-8")
        for provider, model in (("local-brain", "local-brain-v1"), ("local-qwen", "li-huahua-local")):
            block = (
                f"provider: {provider}\n"
                f"        model: {model}\n"
                "        thresholdRatio: 0.68\n"
                "        retainRatio: 0.15\n"
                "        maxTokens: 8192"
            )
            self.assertEqual(policy.count(block), 1)

    def test_settings_example_defaults_to_formal_route(self):
        settings = SETTINGS_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("agent-default-model:\n  provider: local-brain\n  model: local-brain-v1", settings)
        self.assertIn("    local-brain:\n", settings)
        self.assertIn("        - id: local-brain-v1\n", settings)
        self.assertIn("baseURL: http://127.0.0.1:17862/v1", settings)

    def test_settings_example_keeps_legacy_route(self):
        settings = SETTINGS_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("    local-qwen:\n", settings)
        self.assertIn("        - id: li-huahua-local\n", settings)


if __name__ == "__main__":
    unittest.main()
