# Renaming a KubeVirt VirtualMachine on OpenShift (keeping the data disk)

This repo documents how to **rename** a KubeVirt `VirtualMachine` on an OpenShift cluster — e.g. from
`vader.syangsao.net` to `vader` — **without losing the attached PVC / data disk**.

Kubernetes objects do not allow `metadata.name` to be changed in place, so the rename is done by:

1. stopping the VM,
2. protecting the PV (reclaim policy → `Retain`),
3. backing up and editing the VM manifest (new name + labels, server fields stripped),
4. deleting the old VM,
5. re-attaching the PVC to the renamed VM,
6. applying the new VM manifest and verifying the disk is intact.

The procedure below was **tested end-to-end** on the `luke` cluster
(`api-luke-syangsao-net:6443`) renaming `vader.syangsao.net` → `vader` in namespace `virt-linux`.

---

## Why this is necessary / what breaks if you skip a step

A KubeVirt VM's root/data disk is a **PersistentVolumeClaim (PVC)**. The PVC is backed by a
**PersistentVolume (PV)** whose storage class often has `reclaimPolicy: Delete` (e.g. NFS-CSI,
PortWorx). If you delete the old VM *and* its PVC while the PV policy is `Delete`, the underlying
data volume is **destroyed**. That is why step 2 (patching the PV to `Retain`) is mandatory before
deleting anything.

Additionally, on a cluster where the VM was created by an **MTv / forklift migration plan**, deleting
the VM can cause the plan's cleanup controller to set a `deletionTimestamp` on the PVC (it becomes
stuck in `Terminating`). This repo documents how to recover from that too (see
[Recovering a stuck Terminating PVC](#recovering-a-stuck-terminating-pvc)).

---

## Prerequisites

- `oc` (or `kubectl`) with admin access to the target cluster, and `virtctl` on `$PATH`.
- The VM's storage class / PV reclaim policy known (check it — see step 1).
- Enough time: the VM must be powered off for the whole operation.

> **Note:** `oc virtctl …` is *not* a subcommand of `oc`; use the standalone `virtctl` binary instead.

---

## Step-by-step procedure

### 0. Identify the VM, its namespace, PVC and PV

```bash
# Find the VM across all namespaces
oc get vms -A | grep <vm-name>

# Example (our case): VM vader.syangsao.net in namespace virt-linux
NS=virt-linux
VM=vader.syangsao.net
NEW_NAME=vader

# Which PVC does the VM's volume use?
oc get vm $VM -n $NS -o jsonpath='{range .spec.template.spec.volumes[*]}{.name}{"  -> "}{.persistentVolumeClaim.persistentVolumeClaimName}{"\n"}{end}'
```

For our VM the output was:

```
vol-0  type=          # (empty persistentVolumeClaim name in this jsonpath form)
```

so we read the claim directly:

```bash
oc get vm $VM -n $NS -o jsonpath='{.spec.template.spec.volumes[?(@.name=="vol-0")].persistentVolumeClaim.claimName}{"\n"}'
# -> vader-rename-vm-10001-tmmt5
```

Then find the PV bound to that PVC:

```bash
oc get pv | grep <pvc-name>
# pvc-3527684f-4517-49da-aa47-7ee2c7687dce   106Gi   RWX   Delete   Bound   virt-linux/vader-rename-vm-10001-tmmt5   nfs-csi
```

Key facts captured for our VM:

| Item | Value |
|------|-------|
| VM | `vader.syangsao.net` in `virt-linux` |
| New name | `vader` |
| Volume → PVC | `vol-0` → `vader-rename-vm-10001-tmmt5` |
| PV | `pvc-3527684f-4517-49da-aa47-7ee2c7687dce` |
| Storage class | `nfs-csi` (reclaim policy **Delete**) |
| PV capacity / mode | 106Gi, RWX |
| PV source | NFS CSI: `nas01-80.syangsao.lab:/nfsshare/csidriver` |

---

### 1. Stop the VM

```bash
virtctl stop $VM -n $NS
# VM vader.syangsao.net was scheduled to stop
```

Wait until the VMI is gone and the VM shows `Stopped`:

```bash
oc get vmi $VM -n $NS        # -> NotFound (VMI cleaned up)
oc get vm  $VM -n $NS        # STATUS=Stopped
```

> Do **not** proceed while the VM/VMI is still running — the PVC must be released before re-attach.

---

### 2. Patch the PV reclaim policy to `Retain` (MANDATORY)

Because the storage class (`nfs-csi`) uses `reclaimPolicy: Delete`, any deletion of the bound PVC
would delete the data volume. Patch the **PV** (not the PVC) first:

```bash
oc patch pv pvc-3527684f-4517-49da-aa47-7ee2c7687dce \
  -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
# persistentvolume/pvc-3527684f-... patched

# verify
oc get pv pvc-3527684f-4517-49da-aa47-7ee2c7687dce -o jsonpath='{.spec.persistentVolumeReclaimPolicy}{"\n"}'
# Retain
```

If the VM has **multiple** data disks, patch **every** bound PV the same way.

---

### 3. Back up the VM manifest (two files)

Save one copy to edit and one pristine backup:

```bash
oc get vm $VM -n $NS -o yaml > ${VM}_${NS}.yaml.bkp   # actual backup, do not touch
cp ${VM}_${NS}.yaml.bkp ${VM}_${NS}.yaml              # working copy to modify
```

---

### 4. Edit the manifest

In `${VM}_${NS}.yaml`, make these changes:

**Change:**
- `metadata.name` → new name (`vader`)
- `spec.template.metadata.labels.kubevirt.io/domain` → new name (if present)
- `spec.template.metadata.labels.app` → new name (if present)

**Remove** (server-managed fields that would break a re-apply):
- `metadata.uid`
- `metadata.resourceVersion`
- `metadata.creationTimestamp`
- `metadata.generation`
- `metadata.managedFields`
- `metadata.finalizers`
- the entire `status:` block
- `metadata.annotations` (kubemacpool timestamp, `kubevirt.io/latest-observed-api-version`,
  `kubevirt.io/storage-observed-api-version`)

**Keep:**
- `metadata.namespace`
- `metadata.labels` (migration / plan labels etc.)
- **DataVolume / PVC names stay the same** — they are separate resources and must not change.

The edited manifest for our VM (`vader.syangsao.net_virt-linux.yaml`) is committed to this repo as a
worked example. The essential diff:

```diff
 metadata:
-  annotations:
-    kubemacpool.io/transaction-timestamp: "..."
-    kubevirt.io/latest-observed-api-version: v1
-    kubevirt.io/storage-observed-api-version: v1
   labels:
     ...
-  name: vader.syangsao.net
+  name: vader
   namespace: virt-linux
-  resourceVersion: "21232124"
-  uid: c7f445c1-...
 spec:
   template:
     metadata:
       labels:
-        app: vader.syangsao.net
+        app: vader
```

Validate before applying:

```bash
oc apply -f ${VM}_${NS}.yaml --dry-run=client -n $NS
# virtualmachine.kubevirt.io/vader created (dry run)
```

---

### 5. Delete the old VM

```bash
oc delete vm $VM -n $NS
# virtualmachine.kubevirt.io "vader.syangsao.net" deleted
```

**Watch what happens to the PVC.** On a plain cluster the PVC simply stays `Bound`. But if the VM was
created by an **MTv/forklift plan**, the plan cleanup can set a `deletionTimestamp` on the PVC and it
goes to `Terminating`, stuck behind the `kubernetes.io/pvc-protection` finalizer:

```bash
oc get pvc <pvc-name> -n $NS
# NAME                          STATUS        VOLUME   ...
# vader-rename-vm-10001-tmmt5   Terminating   pvc-3527...   106Gi   RWX   nfs-csi

oc get pvc <pvc-name> -n $NS -o jsonpath='{.metadata.deletionTimestamp}{"\n"}{.metadata.finalizers}{"\n"}'
# 2026-10-27T16:59:46Z
# ["kubernetes.io/pvc-protection"]

oc get pv <pv-name> -o jsonpath='{.status.phase}{" "}{.spec.persistentVolumeReclaimPolicy}{"\n"}'
# Bound Retain        # PV is safe; data intact on the backend
```

If you see this, jump to [Recovering a stuck Terminating PVC](#recovering-a-stuck-terminating-pvc)
before applying the new VM. If the PVC simply stayed `Bound`, skip ahead to step 6.

---

### 6. (Only if the PVC went Terminating) Re-attach the PVC to the renamed VM

See the recovery section below. The short version:

1. Remove the `pvc-protection` finalizer so the terminating PVC object is removed.
2. Re-create the PVC with the **same name** and `spec.volumeName` pointing at the (now `Released`) PV.
3. Manually re-bind by patching the PV's `spec.claimRef` to the new PVC's uid.

After this, the PVC is `Bound` again to the same PV and the data disk is intact.

---

### 7. Apply the new (renamed) VM manifest

```bash
oc apply -f ${VM}_${NS}.yaml
# virtualmachine.kubevirt.io/vader created
```

Because the backup was taken while the VM was `Halted`, the new VM starts in `Stopped`. Start it:

```bash
virtctl start vader -n $NS
# VM vader was scheduled to start
```

---

### 8. Verify the VMI started and the data disk is intact

```bash
oc get vm  vader -n $NS     # STATUS=Running READY=True
oc get vmi vader -n $NS     # PHASE=Running NODENAME=control01.syangsao.net READY=True

# Confirm the volume maps to the same PVC
oc get vmi vader -n $NS -o json | python3 -c "import sys,json;d=json.load(sys.stdin);[print(v['name'],'->',v.get('persistentVolumeClaim',{}).get('claimName')) for v in d['spec']['volumes']]"
# vol-0 -> vader-rename-vm-10001-tmmt5

# Confirm the PVC is bound to the same PV
oc get pvc vader-rename-vm-10001-tmmt5 -n $NS
# NAME                          STATUS   VOLUME                                     CAPACITY   ACCESS MODES
# vader-rename-vm-10001-tmmt5   Bound    pvc-3527684f-...                           106Gi      RWX

# Confirm the PV is still Retain + Bound
oc get pv pvc-3527684f-... -o jsonpath='{.status.phase}{" "}{.spec.persistentVolumeReclaimPolicy}{"\n"}'
# Bound Retain

# Confirm the raw disk size on the NFS backend is unchanged (data intact)
ls -la /mnt2/virt-linux-prime-033445b9-...-pvc-3527684f-.../disk.img
# -rw-rw----+ 1 whoopsie bluetooth 107374182400 ... disk.img   (100 GiB, unchanged)
```

For our test the result was:

| Check | Result |
|-------|--------|
| Old VM `vader.syangsao.net` | deleted (NotFound) |
| New VM `vader` | Running, Ready=True on `control01.syangsao.net` |
| VMI volume → PVC | `vol-0 → vader-rename-vm-10001-tmmt5` |
| PVC | Bound to the **same** PV `pvc-3527684f-...`, 106Gi RWX |
| PV | phase `Bound`, reclaim `Retain` |
| Raw disk on NFS | `disk.img` = 107,374,182,400 bytes (unchanged) |

The rename is complete and the data disk survived.

---

## Recovering a stuck Terminating PVC

This section applies when, after deleting the old VM, the PVC goes to `Terminating`
(set by MTv/forklift plan cleanup) and is stuck behind the `kubernetes.io/pvc-protection` finalizer.
The PV is already `Retain`, so the **data on the backend is safe** — we only need to get a usable PVC
back for the renamed VM.

> Prerequisite: confirm no *active* pod still holds the volume (a completed virt-v2v / vddk helper pod
> is harmless). The PV must be `Retain` and not itself have a deletionTimestamp.

### a. Remove the finalizer so the terminating PVC object is removed

```bash
oc patch pvc <pvc-name> -n $NS --type merge -p '{"metadata":{"finalizers":null}}'
# persistentvolumeclaim/<pvc-name> patched
```

The PVC object disappears and the PV transitions to `Released` (still `Retain`, no deletionTimestamp):

```bash
oc get pvc <pvc-name> -n $NS    # NotFound
oc get pv <pv-name> -o jsonpath='{.status.phase}{" "}{.spec.persistentVolumeReclaimPolicy}{"\n"}'
# Released Retain
```

### b. Re-create the PVC bound to the same PV

Create a PVC with the **same name** and `spec.volumeName` pointing at the existing PV:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: <pvc-name>          # same name as before
  namespace: $NS
spec:
  accessModes:
    - ReadWriteMany         # match the original
  storageClassName: nfs-csi # match the original
  volumeName: <pv-name>     # the existing PV (Released)
  resources:
    requests:
      storage: 106Gi        # match the original request
```

```bash
oc apply -f pvc-<name>.yaml
# persistentvolumeclaim/<pvc-name> created
```

The PVC will be `Pending` — Kubernetes does **not** auto-bind a claim to a `Released` PV.

### c. Manually re-bind the PV to the new PVC

Get the new PVC's uid, then patch the PV's `claimRef` to point at it:

```bash
NEW_UID=$(oc get pvc <pvc-name> -n $NS -o jsonpath='{.metadata.uid}')
oc patch pv <pv-name> --type merge \
  -p "{\"spec\":{\"claimRef\":{\"name\":\"<pvc-name>\",\"namespace\":\"$NS\",\"uid\":\"$NEW_UID\"}}}"
# persistentvolume/<pv-name> patched
```

The PV goes back to `Bound` and the PVC becomes `Bound`:

```bash
oc get pvc <pvc-name> -n $NS     # Bound  <pv-name>  106Gi  RWX
oc get pv   <pv-name>            # phase=Bound reclaim=Retain claimRef=$NS/<pvc-name>
```

The data disk is now re-attached to the renamed VM. Continue with step 7 (apply the new VM manifest).

---

## Cleanup (optional)

Remove any leftover completed migration helper pods in the namespace:

```bash
oc get pods -n $NS | grep -E 'Completed' 
# e.g. vader-rename-vm-10001-wprdz, vddk-validator-vader-renamevzc9p-6qxq9
oc delete pod <helper-pod> -n $NS
```

---

## Files in this repo

| File | Purpose |
|------|---------|
| `README.md` | This document. |
| `rename_vm.yml` | Ansible playbook that automates the whole procedure (including the stuck-PVC recovery). |
| `rename_transform.py` | Python helper used by the playbook to rename + strip the VM manifest. Standalone-usable. |
| `vader.syangsao.net_virt-linux.yaml.bkp` | Pristine backup of the original VM manifest (as retrieved from the cluster). |
| `vader.syangsao.net_virt-linux.yaml` | The **edited** manifest used to create the renamed VM `vader` (new name, new `app` label, server fields stripped). |


## Automating with Ansible

The whole procedure is available as an Ansible playbook: `rename_vm.yml`. It performs steps 0–8 above,
including the automatic recovery of a stuck Terminating PVC.

```bash
ansible-playbook rename_vm.yml \
  -e vm_name=vader.syangsao.net \
  -e new_name=vader \
  -e namespace=virt-linux \
  [-e start_after=true] \
  [-e dry_run=false]
```

| Extra var | Default | Meaning |
|-----------|---------|---------|
| `vm_name` | — | current VirtualMachine name (required) |
| `new_name` | — | desired new name (required) |
| `namespace` | — | namespace the VM lives in (required) |
| `start_after` | `true` | power the renamed VM back on at the end |
| `dry_run` | `false` | if `true`: stop + patch PV to Retain only; do **not** delete/apply |

The playbook:

1. reads the live VM, backs up the manifest, and resolves each volume → PVC → PV;
2. stops the VM and waits for the VMI to be gone;
3. patches every bound PV to `Retain` (and asserts it);
4. rewrites the manifest via `rename_transform.py` (rename + strip server fields) and dry-run validates it;
5. deletes the old VM;
6. **automatically** detects any PVC stuck in `Terminating`, removes its finalizer, re-creates the PVC
   bound to the same PV, and manually re-binds the PV's `claimRef`;
7. applies the renamed manifest, starts the VM, and verifies each volume still maps to its original PVC.

> The transform is a plain Python script (`rename_transform.py`), so you can also use it by hand:
>
> ```bash
> oc get vm $VM -n $NS -o yaml | NEW_NAME=$NEW_NAME python3 rename_transform.py > new.yaml
> ```

---

## Quick reference (cheat sheet)

```bash
NS=virt-linux; VM=vader.syangsao.net; NEW_NAME=vader
PVC=vader-rename-vm-10001-tmmt5; PV=pvc-3527684f-4517-49da-aa47-7ee2c7687dce

virtctl stop $VM -n $NS
oc patch pv $PV -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
oc get vm $VM -n $NS -o yaml > ${VM}_${NS}.yaml.bkp
cp ${VM}_${NS}.yaml.bkp ${VM}_${NS}.yaml
# edit ${VM}_${NS}.yaml: name->$NEW_NAME, app label->$NEW_NAME, strip uid/rv/ct/gen/finalizers/status/annotations
oc apply -f ${VM}_${NS}.yaml --dry-run=client -n $NS
oc delete vm $VM -n $NS
# if PVC stuck Terminating: remove finalizer, recreate PVC (volumeName=$PV), patch PV claimRef to new uid
oc apply -f ${VM}_${NS}.yaml
virtctl start $NEW_NAME -n $NS
oc get vmi $NEW_NAME -n $NS   # verify Running + volume -> same PVC
```

---

## Playbook test results (live run on luke)

The playbook was tested end-to-end on the `luke` cluster against a freshly migrated
`vader.syangsao.net` (namespace `virt-linux`, PVC `copy-of-vader-rename2-vm-10001-6vw5d`,
PV `pvc-dcb1d431-da22-44b2-8567-147439d33f52`, nfs-csi, 106Gi). Result: **success** — the VM was
renamed to `vader` and the data disk survived.

| Check | Result |
|-------|--------|
| Old VM `vader.syangsao.net` | deleted (NotFound) |
| New VM `vader` | Running, Ready=True on `control01.syangsao.net` |
| VMI volume → PVC | `vol-0 → copy-of-vader-rename2-vm-10001-6vw5d` (same PVC) |
| PVC / PV | Bound to the same PV, reclaim `Retain`, 106Gi |
| Raw disk on NFS | `disk.img` = 107,374,182,400 bytes (unchanged from baseline) |

### Issues found and fixed during testing

These were real bugs in the first draft of the playbook, all fixed in the committed version:

1. **Playbook structure** — the file was a single mapping; Ansible requires a *list* of plays
   (`- name:` at the top level). Restructured into one play.
2. **`query()` filter unavailable** — `from_yaml | query('json_query', ...)` needs community.general;
   replaced with a plain Jinja attribute expression `(vm_yaml_raw.stdout | from_yaml).spec.template.spec.volumes`.
3. **`changed_when` referencing bare `stdout`/`result`** — in ansible-core 2.21 these are not resolved
   inside `changed_when`. Fixed by adding `register:` to each task and referencing the registered var
   (e.g. `stop_vm.stdout`), or dropping `changed_when` on loop tasks where the command's rc is enough.
4. **`until:` loops failing on non-zero rc** — wait-for-gone tasks need `failed_when: false` so the
   `until` condition (rc != 0 = NotFound) controls the outcome instead of the command failure.
5. **JSON mangled by folded scalars** — `command: >-` strips the double quotes from the `oc patch -p`
   JSON payload. Switched those to block scalars (`|`) with single-quoted JSON.
6. **Shell redirection in `command:`** — the transform task uses `<`/`>` which the `command` module does
   not interpret; switched to `shell:` with `args.executable: /bin/bash`.
7. **PVC re-create missing `spec.resources`** — a PVC requires `spec.resources.requests.storage`; added it
   (captured during discovery) so the recovered PVC is valid.
8. **Final verify jsonpath split by folded scalar** — the `{range ...}` expression contained spaces that
   `>-` collapsed; switched to a block scalar with single quotes.
9. **Re-run after partial failure** — added a fallback so "Read the current VM" uses the saved `.bkp`
   manifest if the source VM is already deleted (makes recovery idempotent).

> Note: on this particular fresh migration the PVC did **not** go stuck-Terminating after the old VM was
> deleted (the plan cleanup left it Bound), so the recovery branch was skipped and the renamed VM attached
> directly. The recovery branch was exercised separately during development and is retained for MTv plans
> that do trigger it.

### Second live run — `windows11.syangsao.net` → `windows11` (virt-windows)

A second end-to-end test was run against a freshly migrated **Windows 11** VM
(`windows11.syangsao.net`, namespace `virt-windows`, PVC `windows11-rename-vm-45007-jxbkw`,
PV `pvc-7ba03116-87a5-4901-8d09-dd31b48ed1f2`, nfs-csi, 106Gi). Result: **success** — and this run
exercised the **stuck-Terminating-PVC recovery branch** for real.

| Check | Result |
|-------|--------|
| Old VM `windows11.syangsao.net` | deleted (NotFound) |
| New VM `windows11` | Running, Ready=True on `control01.syangsao.net`, same IP 192.168.60.55 |
| VMI volume → PVC | `vol-0 → windows11-rename-vm-45007-jxbkw` (same PVC) |
| PVC / PV | Bound to the same PV, reclaim `Retain`, 106Gi |
| Raw disk on NFS | `disk.img` = 107,374,182,400 bytes (unchanged from baseline) |

**What this run added:**

- **Recovery branch proven live.** After the old VM was deleted, MTv plan cleanup set a
  `deletionTimestamp` on the PVC and it went stuck in `Terminating` behind
  `kubernetes.io/pvc-protection`. The playbook detected this, removed the finalizer, re-created the PVC
  bound to the same PV, and manually re-bound the PV's `claimRef` — then applied and started the renamed
  VM. This is the exact path that previously only had a manual walkthrough.
- **New bug fixed — "Delete the old VM" not idempotent.** On a re-run after a partial failure the source
  VM is already gone, so `oc delete vm …` returns rc=1 (NotFound) and failed the play. Added
  `failed_when: false` to that task.
- **Windows shutdown is slow / can hang.** `virtctl stop` sends a graceful ACPI shutdown; this Windows 11
  guest took several minutes and at one point left its launcher pod stuck in `Terminating`, which held the
  VM's deletion finalizer. The data was safe throughout (PV `Retain`). If a Windows VM ever hangs on stop,
  force-delete its `virt-launcher-<vm>` pod to unblock: `oc delete pod <launcher> -n <ns> --grace-period=0 --force`.
- **Cosmetic fix** — a `loop_control label` in "Verify every PV" referenced `item.item.pv` (only valid for the
  nested-results assert task); corrected to `item.pv` to clear the templating warning.
