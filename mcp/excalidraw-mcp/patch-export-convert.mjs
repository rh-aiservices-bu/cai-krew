#!/usr/bin/env node
/**
 * Patches dist/index.js so that export_to_excalidraw converts raw shorthand
 * elements to full Excalidraw scene JSON before encrypting and uploading.
 *
 * Without this patch, LLM clients (Hermes via MCP Gateway) that call
 * export_to_excalidraw with raw checkpoint data produce blank diagrams on
 * excalidraw.com because the shorthand format lacks required properties.
 *
 * The browser widget does this conversion client-side via
 * convertToExcalidrawElements() + serializeAsJSON() from @excalidraw/excalidraw,
 * but that library requires React/DOM and cannot run in Node.js.
 */

import { readFileSync, writeFileSync } from "node:fs";

const file = "dist/index.js";
let code = readFileSync(file, "utf-8");

// The bun bundler renames the destructured `json` param to `json2`
const SEARCH_BUNDLED = "const remappedJson = json2;";
const SEARCH_SOURCE = "const remappedJson = json;";

let search, jsonVar;
if (code.includes(SEARCH_BUNDLED)) {
  search = SEARCH_BUNDLED;
  jsonVar = "json2";
} else if (code.includes(SEARCH_SOURCE)) {
  search = SEARCH_SOURCE;
  jsonVar = "json";
} else {
  console.error("PATCH FAILED: could not find remappedJson assignment in " + file);
  process.exit(1);
}

const replacement = `const remappedJson = (() => {
      const __raw = ${jsonVar};
      function __genId() {
        const c = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        let r = "";
        for (let i = 0; i < 21; i++) r += c[Math.floor(Math.random() * c.length)];
        return r;
      }
      function __convertElements(rawEls) {
        const result = [];
        let idx = 0;
        for (const el of rawEls) {
          if (["cameraUpdate", "delete", "restoreCheckpoint"].includes(el.type)) continue;
          const id = el.id || __genId();
          const seed = el.seed || Math.floor(Math.random() * 2e9);
          const base = {
            id,
            type: el.type,
            x: el.x || 0,
            y: el.y || 0,
            width: el.width || (el.type === "text" ? 100 : 200),
            height: el.height || (el.type === "text" ? 25 : 100),
            angle: el.angle || 0,
            strokeColor: el.strokeColor || "#1e1e1e",
            backgroundColor: el.backgroundColor || "transparent",
            fillStyle: el.fillStyle || "solid",
            strokeWidth: el.strokeWidth || 2,
            strokeStyle: el.strokeStyle || "solid",
            roughness: el.roughness ?? 1,
            opacity: el.opacity ?? 100,
            groupIds: el.groupIds || [],
            frameId: el.frameId || null,
            index: "a" + idx++,
            roundness: el.roundness || (el.type === "diamond" ? { type: 2 } : { type: 3 }),
            seed,
            version: el.version || 1,
            versionNonce: el.versionNonce || Math.floor(Math.random() * 2e9),
            isDeleted: false,
            boundElements: el.boundElements || [],
            updated: Date.now(),
            link: el.link || null,
            locked: el.locked || false,
          };
          if (el.label) {
            const lbl = typeof el.label === "string" ? { text: el.label } : el.label;
            const textId = __genId();
            base.boundElements = [{ id: textId, type: "text" }];
            result.push(base);
            result.push({
              id: textId, type: "text",
              x: base.x + 10, y: base.y + base.height / 2 - 12,
              width: base.width - 20, height: 25,
              angle: 0, strokeColor: base.strokeColor,
              backgroundColor: "transparent", fillStyle: "solid",
              strokeWidth: 1, strokeStyle: "solid", roughness: 1,
              opacity: 100, groupIds: [...base.groupIds],
              frameId: null, index: "a" + idx++, roundness: null,
              seed: Math.floor(Math.random() * 2e9),
              version: 1, versionNonce: Math.floor(Math.random() * 2e9),
              isDeleted: false, boundElements: null,
              updated: Date.now(), link: null, locked: false,
              text: lbl.text, fontSize: lbl.fontSize || 20,
              fontFamily: lbl.fontFamily || 5,
              textAlign: lbl.textAlign || "center",
              verticalAlign: lbl.verticalAlign || "middle",
              containerId: id, originalText: lbl.text,
              autoResize: true, lineHeight: 1.25,
            });
          } else if (el.type === "text") {
            Object.assign(base, {
              text: el.text || "", fontSize: el.fontSize || 20,
              fontFamily: el.fontFamily || 5,
              textAlign: el.textAlign || "left",
              verticalAlign: el.verticalAlign || "top",
              containerId: el.containerId || null,
              originalText: el.originalText || el.text || "",
              autoResize: true, lineHeight: el.lineHeight || 1.25,
              roundness: null,
            });
            result.push(base);
          } else if (el.type === "arrow" || el.type === "line") {
            Object.assign(base, {
              points: el.points || [[0, 0], [base.width, 0]],
              startBinding: el.startBinding || null,
              endBinding: el.endBinding || null,
              lastCommittedPoint: null,
              startArrowhead: el.startArrowhead || null,
              endArrowhead: el.type === "arrow" ? (el.endArrowhead ?? "arrow") : null,
            });
            if (!el.width && !el.height && el.points) {
              const xs = el.points.map(p => p[0]);
              const ys = el.points.map(p => p[1]);
              base.width = Math.max(...xs) - Math.min(...xs);
              base.height = Math.max(...ys) - Math.min(...ys);
            }
            result.push(base);
          } else {
            result.push(base);
          }
        }
        return result;
      }
      try {
        const parsed = JSON.parse(__raw);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)
            && parsed.type === "excalidraw" && parsed.version) {
          return __raw;
        }
        let rawElements;
        if (Array.isArray(parsed)) {
          rawElements = parsed;
        } else if (parsed && parsed.elements && Array.isArray(parsed.elements)) {
          rawElements = parsed.elements;
        } else {
          return __raw;
        }
        return JSON.stringify({
          type: "excalidraw", version: 2,
          source: "https://excalidraw.com",
          elements: __convertElements(rawElements),
          appState: { gridSize: null, viewBackgroundColor: "#ffffff" },
          files: {},
        });
      } catch (_e) {
        return __raw;
      }
    })();`;

code = code.replace(search, replacement);
writeFileSync(file, code);
console.log("Patched export_to_excalidraw with shorthand element conversion");
