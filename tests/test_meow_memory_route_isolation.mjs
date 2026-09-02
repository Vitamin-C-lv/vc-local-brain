import assert from 'node:assert/strict'
import test from 'node:test'

import {
  filterPersistentMemoryAssembly,
  isPersistentMemoryAllowed,
  isPersistentMemoryAllowedRoute,
  isPersistentMemoryTool,
  routeIdentity,
} from '../dsh/meow-memory-route-isolation.mjs'

const localBrain = { provider: 'local-brain', model: 'local-brain-v1' }
const legacyLocal = { provider: 'local-qwen', model: 'li-huahua-local' }
const deepSeek = { provider: 'deepseek-official', model: 'deepseek-v4-flash' }
const unknown = { provider: 'future-local', model: 'future-model' }

const agentFor = (route) => ({ session: { header: { config: route } } })

test('LOCAL_BRAIN_MEMORY_SYSTEM_PROMPT_ABSENT', () => {
  const assembly = filterPersistentMemoryAssembly({
    sections: [
      { name: 'meow-memory:guide', text: '长期记忆 memory_remember' },
      { name: 'persona', text: 'normal' },
    ],
    tools: [{ name: 'memory_search' }, { name: 'bash' }],
    contexts: [{ source: { plugin: 'meow-memory' } }, { name: 'runtime' }],
  }, agentFor(localBrain))
  assert.deepEqual(assembly.sections.map((section) => section.name), ['persona'])
  assert.deepEqual(assembly.contexts.map((context) => context.name), ['runtime'])
})

test('LOCAL_BRAIN_MEMORY_TOOLS_ABSENT', () => {
  const assembly = filterPersistentMemoryAssembly({
    sections: [],
    tools: [{ name: 'memory_search' }, { name: 'memory_dream' }, { name: 'bash' }],
  }, agentFor(localBrain))
  assert.deepEqual(assembly.tools.map((tool) => tool.name), ['bash'])
})

test('LEGACY_LOCAL_QWEN_MEMORY_TOOLS_ABSENT', () => {
  const assembly = filterPersistentMemoryAssembly({
    sections: [],
    tools: [{ name: 'memory_read' }, { name: 'bash' }],
  }, agentFor(legacyLocal))
  assert.deepEqual(assembly.tools.map((tool) => tool.name), ['bash'])
})

test('LOCAL_BRAIN_AUTO_RECALL_DISABLED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(localBrain)), false)
})

test('LOCAL_BRAIN_REFLECTION_DISABLED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(localBrain)), false)
})

test('LOCAL_BRAIN_DREAM_DISABLED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(localBrain)), false)
})

test('DEEPSEEK_MEMORY_SYSTEM_PROMPT_PRESENT', () => {
  const assembly = {
    sections: [{ name: 'meow-memory:guide', text: '长期记忆' }],
    tools: [{ name: 'memory_search' }],
  }
  assert.strictEqual(filterPersistentMemoryAssembly(assembly, agentFor(deepSeek)), assembly)
})

test('DEEPSEEK_MEMORY_TOOLS_PRESENT', () => {
  assert.equal(isPersistentMemoryTool('memory_search'), true)
  assert.strictEqual(
    filterPersistentMemoryAssembly({ tools: [{ name: 'memory_search' }] }, agentFor(deepSeek)).tools[0].name,
    'memory_search',
  )
})

test('MODEL_SELECTION_VARIABLES_ROUTE_ASSEMBLY', () => {
  const assembly = {
    variables: deepSeek,
    sections: [{ name: 'meow-memory:guide', text: '长期记忆' }],
    tools: [{ name: 'memory_search' }],
  }
  assert.deepEqual(routeIdentity(assembly), deepSeek)
  assert.strictEqual(filterPersistentMemoryAssembly(assembly, assembly), assembly)
})

test('DEEPSEEK_AUTO_RECALL_UNCHANGED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(deepSeek)), true)
})

test('DEEPSEEK_REFLECTION_UNCHANGED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(deepSeek)), true)
})

test('DEEPSEEK_DREAM_UNCHANGED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(deepSeek)), true)
})

test('UNKNOWN_PROVIDER_MEMORY_DENIED', () => {
  assert.equal(isPersistentMemoryAllowed(agentFor(unknown)), false)
  assert.equal(isPersistentMemoryAllowedRoute('deepseek-official', 'unknown-model'), false)
  assert.deepEqual(routeIdentity({ header: { config: unknown } }), unknown)
})
