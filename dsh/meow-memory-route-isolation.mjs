/**
 * DSH meow-memory route policy.
 *
 * Persistent memory is an allowlist capability: only the official DeepSeek
 * adapter route may use it. Session context and compaction remain independent
 * from this policy and are not represented here.
 */

export const PERSISTENT_MEMORY_POLICY = 'DEEPSEEK_OFFICIAL_ONLY'
export const OFFICIAL_DEEPSEEK_PROVIDER = 'deepseek-official'
export const OFFICIAL_DEEPSEEK_MODELS = Object.freeze([
  'deepseek-v4-flash',
  'deepseek-v4-pro',
  'deepseek-v4-flash-vision-exp',
])

const OFFICIAL_MODEL_SET = new Set(OFFICIAL_DEEPSEEK_MODELS)

/** Extract the route identity from an agent, tool execution, or assemble context. */
export function routeIdentity(value) {
  const candidate = value?.agent ?? value
  const header = candidate?.session?.header ?? candidate?.header
  const candidates = [
    value?.variables,
    value?.route,
    candidate?.options,
    header?.config,
    candidate?.config,
    value?.config,
  ]
  const config = candidates.find(
    (item) => typeof item?.provider === 'string' && typeof item?.model === 'string',
  )
  return {
    provider: typeof config?.provider === 'string' ? config.provider : null,
    model: typeof config?.model === 'string' ? config.model : null,
  }
}

/** The sole persistent-memory allowlist decision. Unknown routes deny. */
export function isPersistentMemoryAllowedRoute(provider, model) {
  return provider === OFFICIAL_DEEPSEEK_PROVIDER && OFFICIAL_MODEL_SET.has(model)
}

export function isPersistentMemoryAllowed(value) {
  const route = routeIdentity(value)
  return isPersistentMemoryAllowedRoute(route.provider, route.model)
}

export function isPersistentMemoryTool(name) {
  return typeof name === 'string' && name.startsWith('memory_')
}

function isMeowMemorySection(section) {
  return typeof section?.name === 'string' && section.name.startsWith('meow-memory:')
}

function isMeowMemoryContext(context) {
  return (
    typeof context?.name === 'string' && context.name.startsWith('meow-memory:')
  ) || context?.source?.plugin === 'meow-memory'
}

/** Remove all model-facing meow-memory contributions for a denied route. */
export function filterPersistentMemoryAssembly(assembly, routeContext) {
  if (isPersistentMemoryAllowed(routeContext)) return assembly
  const filtered = { ...assembly }
  if (Array.isArray(filtered.sections)) {
    filtered.sections = filtered.sections.filter((section) => !isMeowMemorySection(section))
  }
  if (Array.isArray(filtered.tools)) {
    filtered.tools = filtered.tools.filter((tool) => !isPersistentMemoryTool(tool?.name))
  }
  if (Array.isArray(filtered.contexts)) {
    filtered.contexts = filtered.contexts.filter((context) => !isMeowMemoryContext(context))
  }
  return filtered
}
