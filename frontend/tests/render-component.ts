import ts from "typescript";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";

export function loadComponent(
  file: string | URL,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const url = new URL(file, import.meta.url);
  const resolve = createRequire(url);
  const source = readFileSync(url, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const module = { exports: {} };
  const require = (name: string) => {
    if (name in overrides) return overrides[name];
    if (name.endsWith(".css")) return {};
    if (name.startsWith(".")) {
      for (const suffix of [".ts", ".tsx"]) {
        const dependency = new URL(name + suffix, url);
        if (existsSync(dependency)) return loadComponent(dependency, overrides);
      }
    }
    return resolve(name);
  };
  new Function("require", "module", "exports", compiled)(
    require,
    module,
    module.exports,
  );
  return module.exports;
}
