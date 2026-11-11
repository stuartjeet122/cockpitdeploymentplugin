import { build } from "esbuild";

const icons = [
  "Activity", "ArrowLeft", "ArrowRight", "ArrowUpRight", "Boxes", "Check", "CircleAlert",
  "CircleCheck", "Clock", "Download", "Ellipsis", "ExternalLink", "Eye", "EyeOff", "FileText",
  "GitBranch", "Globe", "KeyRound", "LayoutGrid", "LoaderCircle", "Pause", "Play", "Plus",
  "RefreshCw", "Search", "Settings2", "ShieldCheck", "SquareTerminal", "Trash2", "Upload", "X",
];

await build({
  stdin: {
    contents: `import { createIcons, ${icons.join(", ")} } from "lucide";
      globalThis.renderAppManagerIcons = () => createIcons({
        icons: { ${icons.join(", ")} },
        attrs: { "aria-hidden": "true", "stroke-width": "1.75" },
      });`,
    resolveDir: process.cwd(),
  },
  bundle: true,
  minify: true,
  charset: "ascii",
  legalComments: "inline",
  banner: { js: "/* Cockpit App Manager offline icons. */" },
  outfile: "cockpit/icons.js",
});