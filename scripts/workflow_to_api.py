#!/usr/bin/env python3
"""Flatten a ComfyUI *UI* workflow JSON (LiteGraph format, subgraphs supported) into an
*API prompt* JSON ({node_id: {"class_type", "inputs"}}) that can be POSTed to /prompt.

Why: the official MiniMax H3 templates wrap the pipeline in a subgraph; the ComfyUI server
has no UI->API conversion, so headless/autonomous runs need this flattening. Rules mirror
the frontend's graphToPrompt: inner nodes get ids "<instance>:<inner>", subgraph inputs
resolve to the instance's links or promoted widget values, bypassed nodes (mode 4) pass
their input through by type, muted nodes (mode 2) are dropped, and only nodes reachable
from an active OUTPUT node are emitted. Widget values are mapped to input names using the
live /object_info of the target server (or a saved copy), so it stays correct for the
exact ComfyUI version in use.

Usage:
  workflow_to_api.py --workflow wf.json --server http://127.0.0.1:8188 [--out api.json]
  workflow_to_api.py --workflow wf.json --object-info object_info.json --out api.json
Stdlib only.
"""
import argparse
import json
import sys
import urllib.request

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COMFY_DYNAMICCOMBO_V3"}
DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
AUTOGROW = "COMFY_AUTOGROW_V3"
SUBGRAPH_INPUT_NODE = -10
SUBGRAPH_OUTPUT_NODE = -20
MODE_MUTE = 2
MODE_BYPASS = 4


def load_object_info(server=None, path=None):
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    if not server:
        raise SystemExit("need --server or --object-info")
    with urllib.request.urlopen(server.rstrip("/") + "/object_info", timeout=60) as r:
        return json.load(r)


def types_compatible(a, b):
    if a is None or b is None:
        return True
    a, b = str(a), str(b)
    if a == b or a == "*" or b == "*":
        return True
    sa, sb = set(a.split(",")), set(b.split(","))
    return bool(sa & sb)


class Scope:
    """One graph level: the top-level workflow or one subgraph *instance*."""

    def __init__(self, nodes, links, prefix, parent=None, instance=None, definition=None):
        self.nodes = {n["id"]: n for n in nodes}
        self.links = {}
        for l in links or []:
            if isinstance(l, dict):
                self.links[l["id"]] = (l["origin_id"], l["origin_slot"], l["target_id"], l["target_slot"], l.get("type"))
            else:
                self.links[l[0]] = (l[1], l[2], l[3], l[4], l[5] if len(l) > 5 else None)
        self.prefix = prefix          # "" for top level, else "<instance flat id>:"
        self.parent = parent
        self.instance = instance      # instance node dict (in parent scope) for a subgraph
        self.definition = definition  # subgraph definition dict
        self.children = {}            # instance node id -> Scope


class Flattener:
    def __init__(self, workflow, object_info):
        self.wf = workflow
        self.oi = object_info
        self.subgraph_defs = {}
        for sg in ((workflow.get("definitions") or {}).get("subgraphs") or []):
            self.subgraph_defs[sg["id"]] = sg
        self.root = self._build_scope(workflow["nodes"], workflow.get("links") or [], "", None, None, None)
        self.warnings = []

    def _build_scope(self, nodes, links, prefix, parent, instance, definition):
        scope = Scope(nodes, links, prefix, parent, instance, definition)
        for n in nodes:
            sg = self.subgraph_defs.get(n["type"])
            if sg is not None:
                child_prefix = f"{prefix}{n['id']}:"
                scope.children[n["id"]] = self._build_scope(sg["nodes"], sg.get("links") or [], child_prefix, scope, n, sg)
        return scope

    # ---- helpers -------------------------------------------------------
    def flat_id(self, scope, node_id):
        return f"{scope.prefix}{node_id}"

    def is_subgraph_instance(self, node):
        return node["type"] in self.subgraph_defs

    def instance_widget_value(self, scope, sg_input_index):
        """Promoted widget value of a subgraph instance for subgraph input #index."""
        sg = scope.definition
        inst = scope.instance
        sg_input = sg["inputs"][sg_input_index]
        name = sg_input["name"]
        named = inst.get("widgets_values_named")
        if isinstance(named, dict) and name in named:
            return True, named[name]
        # positional fallback: widget-typed subgraph inputs in order consume widgets_values
        widget_inputs = [i for i in sg["inputs"] if str(i.get("type")) in WIDGET_TYPES or str(i.get("type")) == "COMBO"]
        vals = inst.get("widgets_values") or []
        for k, i in enumerate(widget_inputs):
            if i is sg_input:
                if k < len(vals):
                    return True, vals[k]
                break
        return False, None

    def resolve_link(self, scope, link_id):
        if link_id is None or link_id not in scope.links:
            return None
        origin_id, origin_slot, _t, _s, ltype = scope.links[link_id]
        return self.resolve_source(scope, origin_id, origin_slot, ltype)

    def resolve_source(self, scope, origin_id, origin_slot, ltype=None):
        """Returns ("link", flat_id, slot) | ("value", v) | None."""
        if origin_id == SUBGRAPH_INPUT_NODE:
            sg_input = scope.definition["inputs"][origin_slot]
            inst = scope.instance
            parent = scope.parent
            for inp in inst.get("inputs") or []:
                if inp.get("name") == sg_input["name"]:
                    if inp.get("link") is not None:
                        return self.resolve_link(parent, inp["link"])
                    break
            ok, v = self.instance_widget_value(scope, origin_slot)
            return ("value", v) if ok else None
        node = scope.nodes.get(origin_id)
        if node is None:
            return None
        mode = node.get("mode", 0)
        if mode == MODE_MUTE:
            return None
        if self.is_subgraph_instance(node):
            child = scope.children[node["id"]]
            for lid, (o_id, o_slot, t_id, t_slot, lt) in child.links.items():
                if t_id == SUBGRAPH_OUTPUT_NODE and t_slot == origin_slot:
                    return self.resolve_source(child, o_id, o_slot, lt)
            return None
        if mode == MODE_BYPASS:
            outputs = node.get("outputs") or []
            out_type = outputs[origin_slot].get("type") if origin_slot < len(outputs) else ltype
            inputs = node.get("inputs") or []
            candidates = []
            if origin_slot < len(inputs):
                candidates.append(inputs[origin_slot])
            candidates += [i for i in inputs if i is not (inputs[origin_slot] if origin_slot < len(inputs) else None)]
            for inp in candidates:
                if types_compatible(inp.get("type"), out_type):
                    if inp.get("link") is not None:
                        return self.resolve_link(scope, inp["link"])
                    return None
            return None
        return ("link", self.flat_id(scope, origin_id), origin_slot)

    # ---- widget mapping -------------------------------------------------
    @staticmethod
    def _is_widget(t, cfg):
        if isinstance(t, list):
            return True
        if cfg.get("forceInput") or cfg.get("force_input"):
            return False
        return str(t) in WIDGET_TYPES

    def _consume_inputs(self, spec_inputs, order, node, widgets, out, link_by_name, prefix=None):
        """Walk inputs in order, consuming widgets_values and resolving links."""
        for section in ("required", "optional"):
            names = (order or {}).get(section) if order else None
            if names is None:
                names = list((spec_inputs.get(section) or {}).keys())
            for name in names:
                spec = (spec_inputs.get(section) or {}).get(name)
                if spec is None:
                    continue
                t = spec[0]
                cfg = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                key = f"{prefix}.{name}" if prefix else name
                if str(t) == AUTOGROW:
                    # values only via links; keys look like "values.a"
                    for lname, lval in link_by_name.items():
                        if lname.startswith(name + "."):
                            out[lname] = lval
                    continue
                if self._is_widget(t, cfg):
                    has_value = bool(widgets)
                    value = widgets.pop(0) if widgets else cfg.get("default")
                    if cfg.get("control_after_generate") not in (None, False) and widgets:
                        widgets.pop(0)  # 'fixed'/'randomize' control widget
                    if (cfg.get("image_upload") or cfg.get("video_upload") or cfg.get("audio_upload")) and widgets:
                        widgets.pop(0)  # upload button widget
                    if key in link_by_name:
                        out[key] = link_by_name[key]
                    elif has_value or "default" in cfg or section == "required":
                        if value is None and section == "optional":
                            continue
                        out[key] = value
                    if str(t) == DYNAMIC_COMBO:
                        sel = None
                        for opt in cfg.get("options") or []:
                            if opt.get("key") == value:
                                sel = opt
                                break
                        if sel is not None:
                            self._consume_inputs(sel.get("inputs") or {}, None, node, widgets, out, link_by_name, prefix=key)
                else:
                    if key in link_by_name:
                        out[key] = link_by_name[key]

    def build_node_inputs(self, scope, node):
        ctype = node["type"]
        info = self.oi.get(ctype)
        if info is None:
            raise SystemExit(f"node type not registered on server: {ctype} (node {self.flat_id(scope, node['id'])})")
        link_by_name = {}
        for inp in node.get("inputs") or []:
            if inp.get("link") is None:
                continue
            src = self.resolve_link(scope, inp["link"])
            if src is None:
                continue
            if src[0] == "link":
                link_by_name[inp["name"]] = [src[1], src[2]]
            else:
                link_by_name[inp["name"]] = src[1]
        widgets = list(node.get("widgets_values") or []) if isinstance(node.get("widgets_values"), list) else []
        out = {}
        self._consume_inputs(info.get("input") or {}, info.get("input_order"), node, widgets, out, link_by_name)
        if widgets:
            self.warnings.append(f"{ctype} {self.flat_id(scope, node['id'])}: {len(widgets)} unused widget value(s): {widgets}")
        return out

    # ---- main -----------------------------------------------------------
    def flatten(self):
        prompt = {}
        visited = set()

        def visit(scope, node):
            fid = self.flat_id(scope, node["id"])
            if fid in visited:
                return
            visited.add(fid)
            inputs = self.build_node_inputs(scope, node)
            prompt[fid] = {"class_type": node["type"], "inputs": inputs}
            title = node.get("title")
            if title:
                prompt[fid]["_meta"] = {"title": title}
            for v in inputs.values():
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int):
                    self._visit_flat(v[0], visit)

        self._visit_flat_cache = {}
        # collect all active output nodes from every scope
        outputs = []
        def collect(scope):
            for n in scope.nodes.values():
                if n.get("mode", 0) in (MODE_MUTE, MODE_BYPASS):
                    continue
                if self.is_subgraph_instance(n):
                    collect(scope.children[n["id"]])
                    continue
                info = self.oi.get(n["type"])
                if info and info.get("output_node"):
                    outputs.append((scope, n))
        collect(self.root)
        if not outputs:
            raise SystemExit("workflow has no active OUTPUT node")
        for scope, n in outputs:
            visit(scope, n)
        return prompt

    def _visit_flat(self, fid, visit):
        scope, node = self._locate(fid)
        visit(scope, node)

    def _locate(self, fid):
        parts = fid.split(":")
        scope = self.root
        for p in parts[:-1]:
            scope = scope.children[int(p)]
        node = scope.nodes[int(parts[-1])]
        return scope, node


def convert(workflow, object_info):
    fl = Flattener(workflow, object_info)
    prompt = fl.flatten()
    return prompt, fl.warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--server")
    ap.add_argument("--object-info")
    ap.add_argument("--out")
    a = ap.parse_args()
    with open(a.workflow, "r", encoding="utf-8") as f:
        wf = json.load(f)
    if "nodes" not in wf:
        raise SystemExit("input is not a UI workflow (no 'nodes'); API prompts need no conversion")
    oi = load_object_info(a.server, a.object_info)
    prompt, warnings = convert(wf, oi)
    for w in warnings:
        print("WARNING:", w, file=sys.stderr)
    text = json.dumps(prompt, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {a.out}: {len(prompt)} nodes")
    else:
        print(text)


if __name__ == "main" or __name__ == "__main__":
    main()
