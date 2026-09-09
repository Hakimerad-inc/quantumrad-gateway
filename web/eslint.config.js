import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// M8: the Python gates (ruff/mypy) previously stopped at the language
// boundary. This config gives the SPA a matching lint gate.
export default tseslint.config(
  { ignores: ["dist", "coverage", "src/mercure_gateway", "node_modules"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // The API layer intentionally narrows untyped fetch bodies (`as T` at
      // documented sites); banning outright would just paper the file with
      // disables. `no-unused-vars` is covered by the TS rule.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);
