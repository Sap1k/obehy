import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "src/api/schema.d.ts"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Browser globals that read like local names: a missing variable must not resolve to one
      // (an undestructured `stop` became `window.stop`).
      "no-restricted-globals": [
        "error",
        ...["stop", "name", "close", "open", "status", "event", "top", "parent", "origin"],
        ...["length", "focus", "blur", "find", "print", "scroll", "self", "external"],
      ],
    },
  },
);
