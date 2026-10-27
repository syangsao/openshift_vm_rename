#!/usr/bin/env python3
"""
rename_transform.py — turn an original KubeVirt VM manifest into a re-applicable one.

Reads the original manifest from stdin, writes the renamed manifest to stdout.
Changes exactly what is needed for a rename and strips server-managed fields:

  * metadata.name                          -> NEW_NAME
  * spec.template.metadata.labels.app     -> NEW_NAME   (if present)
  * spec.template.metadata.labels.kubevirt.io/domain -> NEW_NAME (if present)
  * removed: metadata.uid, resourceVersion, creationTimestamp, generation,
             managedFields, finalizers, annotations, and the whole status block
Keeps: namespace, labels, preference, runStrategy, template spec, volume/PVC names.

Usage:
    oc get vm <name> -n <ns> -o yaml | NEW_NAME=<new> python3 rename_transform.py > new.yaml
"""
import sys
import os
import yaml


def transform(doc, new_name):
    meta = doc.setdefault("metadata", {})

    # Rename
    meta["name"] = new_name

    # Strip server-managed metadata fields
    for key in ("uid", "resourceVersion", "creationTimestamp", "generation",
                "managedFields", "finalizers", "annotations"):
        meta.pop(key, None)

    # Drop the status block entirely
    doc.pop("status", None)

    # Rename the template labels that encode the VM identity
    tlabels = (doc.get("spec", {})
               .get("template", {})
               .get("metadata", {})
               .get("labels", {}))
    for key in ("app", "kubevirt.io/domain"):
        if key in tlabels:
            tlabels[key] = new_name

    return doc


def main():
    new_name = os.environ.get("NEW_NAME")
    if not new_name:
        sys.stderr.write("ERROR: NEW_NAME env var is required\n")
        return 2

    raw = sys.stdin.read()
    doc = yaml.safe_load(raw)
    out = transform(doc, new_name)
    sys.stdout.write(yaml.safe_dump(out, sort_keys=False, default_flow_style=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
