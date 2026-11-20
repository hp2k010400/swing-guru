// Test-only resolution hook: Node's ESM resolver needs explicit extensions, while the app imports
// its modules the Metro/TypeScript way (`./billing-core`). This appends `.ts` for extensionless
// relative specifiers so `node --test` can import the same source files unchanged.
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('.') && !/\.[cm]?[jt]sx?$/i.test(specifier)) {
    try {
      return await nextResolve(`${specifier}.ts`, context);
    } catch {
      /* fall through to the default resolution */
    }
  }
  return nextResolve(specifier, context);
}
