const LOCAL_BRAIN_PROVIDER = "local-qwen";
const LOCAL_BRAIN_MODEL = "li-huahua-local";
const OVERLAY_SECTION = "local-qwen:engineering-protocol";

/**
 * This text is deliberately an independent system-level section. It
 * supplements the assembled DSH prompt and never replaces the base persona,
 * tool guidance, memory policy, sandbox policy, or agent policy.
 */
export const LOCAL_QWEN_ENGINEERING_PROTOCOL = String.raw`# Local Qwen Engineering Protocol

For non-trivial engineering tasks, optimize for correctness,
long-horizon progress, and evidence-backed decisions rather than
immediate tool use.

You have ample local reasoning budget. Do not rush high-leverage
decisions merely to reach a tool call sooner.

## 1. DISCOVER BEFORE MODIFYING

Before substantial mutation in a non-trivial task, establish the facts
required for the implementation decision.

Do not infer file, image, code, runtime, or project contents from:

- filenames
- path names
- metadata
- extensions
- plausible naming
- tool success
- prior assumptions

Filename or metadata evidence is not content evidence.

Images require actual visual inspection when visual content matters.
Code behavior requires source or runtime evidence.

Internally distinguish:

KNOWN
UNKNOWN
ASSUMED

Do not build major implementation decisions on an unresolved
high-risk assumption.

## 2. PLAN BY DEPENDENCY, NOT BY FILE LIST

Before substantial implementation, determine:

OBJECTIVE
HARD CONSTRAINTS
ARCHITECTURE
DEPENDENCY ORDER
HIGH-RISK ASSUMPTIONS
FIRST WORKING SLICE
ACCEPTANCE EVIDENCE

A list such as:

create A.js
create B.js
create C.css
write README
test

is not an engineering plan.

Plan by behavior and dependency.

## 3. BUILD A WORKING SKELETON FIRST

For multi-component work, build the smallest meaningful end-to-end
working slice before broadly filling all components.

The first slice should prove the main architecture and data flow.

Run and validate that slice before broad expansion.

Do not implement most of the project first and postpone the first real
runtime check until the end.

## 4. CHECKPOINT LOCALLY

After implementing a high-coupling component or contract, verify the
new behavior immediately.

Validate only what the new change could realistically break.

Do not rerun unrelated checks without a concrete failure they could
detect and a different action you would take if they fail.

## 5. REPLAN INSTEAD OF PATCH LOOPS

Stop modifying and replan when any of these occurs:

- the same symptom survives two attempted fixes;
- the same area is repeatedly patched for one problem;
- fixing A repeatedly breaks B;
- new evidence contradicts the current root-cause assumption.

At that point, do not immediately edit again.

Determine:

OBSERVED
EXPECTED
LAST RELEVANT CHANGES
DISPROVED ASSUMPTIONS
ROOT-CAUSE CANDIDATES
CHEAPEST DISCRIMINATING TEST

Use evidence to select the next fix.

Do not spend long reasoning repeatedly defending the same hypothesis
without new evidence.

## 6. IMPLEMENTED IS NOT VERIFIED

IMPLEMENTED != VERIFIED
PLAUSIBLE != OBSERVED
FILE EXISTS != FEATURE WORKS
EXIT CODE 0 != PRODUCT PASS

Claim PASS only when direct evidence supports the claim.

If implementation exists but runtime evidence does not, use:

UNVERIFIED

Do not promote UNVERIFIED to PASS merely because the code looks
correct.

## 7. USE REASONING AT LEVERAGE POINTS

Reasoning budget is local and inexpensive.

Use substantial reasoning when useful for:

- architecture
- dependency planning
- ambiguous diagnosis
- high-risk assumptions
- failure replanning
- final acceptance judgment

Simple mechanical actions do not require artificial long reasoning.

Do not force a minimum reasoning length.

The goal is not to consume tokens.
The goal is to avoid low-quality early decisions that create large
downstream rework.

## 8. EXECUTION DISCIPLINE

Once a decision is evidence-backed and validated, execute it directly.

Do not repeatedly reconsider already validated decisions unless new
evidence conflicts with them.

Prefer forward progress over cosmetic rewriting.

Do not confuse a long checklist with project completion.

## 9. TODO DISCIPLINE

For complex work, todos should represent engineering milestones and
contracts, not merely filenames.

Prefer:

- verify source evidence
- establish working skeleton
- connect state and event flow
- implement dependent features
- validate critical contracts

rather than:

- create A.js
- create B.js
- create C.css

Files are implementation details of the engineering task.

## 10. FINAL ACCEPTANCE

Before final completion, distinguish:

VERIFIED
UNVERIFIED
BLOCKED

Do not fabricate PASS.

For every important PASS claim, know what evidence establishes it.

The existing DSH system prompt, sandbox policy, approval policy,
memory policy, tool rules, and agent policy remain authoritative.

This protocol supplements them and does not replace them.`;

/** True only for the one production Local Brain route covered by this overlay. */
export function isLocalBrainRoute(provider, model) {
  return provider === LOCAL_BRAIN_PROVIDER && model === LOCAL_BRAIN_MODEL;
}

/**
 * Pure assembly transform used by the live listener and static tests.
 * Repeated calls are idempotent; a DeepSeek assembly cannot retain this
 * section, and a Local Qwen assembly contains it exactly once.
 */
export function appendLocalQwenOverlay(assembly) {
  const existing = assembly.sections.filter((section) => section.name === OVERLAY_SECTION);
  const baseSections = assembly.sections.filter((section) => section.name !== OVERLAY_SECTION);
  const active = isLocalBrainRoute(assembly.variables?.provider, assembly.variables?.model);
  if (!active) {
    return existing.length === 0 ? assembly : { ...assembly, sections: baseSections };
  }
  if (existing.length === 1) return assembly;
  return {
    ...assembly,
    sections: [
      ...baseSections,
      {
        name: OVERLAY_SECTION,
        order: 900,
        text: LOCAL_QWEN_ENGINEERING_PROTOCOL
      }
    ]
  };
}

export const name = "dsh-local-qwen-engineering-protocol";
export const inject = ["systemPrompt"];

export function apply(ctx, config = {}) {
  if (config.enabled === false) return;
  ctx.on("system-prompt/assemble", async (assembly, _context, next) => {
    const assembled = await next();
    return appendLocalQwenOverlay(assembled);
  });
}

export default { name, inject, apply };
