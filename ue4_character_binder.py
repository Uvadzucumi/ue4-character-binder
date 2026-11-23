#!/usr/bin/env python3
# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# UE4 Character Binder
# --------------------------------------------------------------------
# Workflow (temporary-pose binding, rest pose of the UE4 skeleton is
# NEVER modified):
#   1. Import UE4 mannequin (mesh + armature) from UE4 FBX export.
#   2. Import your custom character mesh (A-pose), scale it by hand
#      to roughly match the mannequin.
#   3. Delete the mannequin MESH (keep the armature).
#   4. Run this addon, steps 1-7 (see panel "UE4 Binder").
#   5. Export FBX, in UE4 import with "Use Existing Skeleton".
#
# Key idea: the skeleton is temporarily posed to match the character
# (markers are moved onto the character joints first), auto weights
# are computed in that bent pose (bone heat uses current bone
# positions), then the pose is reset to the standard UE4 rest pose.
# The character keeps its authored shape (e.g. a hunched goblin),

# the skeleton keeps the standard rest pose, so the exported FBX is
# fully compatible with the project skeleton and its animations
# without any retargeting.
#
# Markers are minimal: NO chain-end markers. Every joint (including
# the last bone's head) is snapped EXACTLY onto its marker: each bone
# is translated, rotated and stretched (UNIFORM pose scale only, which
# never shears the hierarchy). The last bone of every chain keeps the
# mannequin joint angle - only its head snaps onto its marker. Thumb
# chains are optional (off by default): thumbs simply follow the posed
# hand. To let pose mode move every joint independently, chain bones
# are temporarily detached from their parent tails (use_connect=False,
# rest geometry unchanged); the bind/reset steps restore them.

bl_info = {
    "name": "UE4 Character Binder",
    "author": "Uvadzucumi",
    "version": (1, 5, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > UE4 Binder",
    "description": "Bind custom character to standard UE4 skeleton via temporary pose. Rest pose untouched - no retargeting needed.",
    "category": "Rigging",
}

import json
import re

import bmesh
import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

MARKER_COLLECTION_NAME = "UE4_JointMarkers"
MARKER_PREFIX = "M_"

# Bones that must exist in a standard UE4 mannequin skeleton.
REQUIRED_BONES = (
    'pelvis', 'spine_01', 'spine_02', 'spine_03', 'neck_01', 'head',
    'clavicle_l', 'upperarm_l', 'lowerarm_l', 'hand_l',
    'clavicle_r', 'upperarm_r', 'lowerarm_r', 'hand_r',
    'thigh_l', 'calf_l', 'foot_l', 'ball_l',
    'thigh_r', 'calf_r', 'foot_r', 'ball_r',
)

# Bones that always receive a marker at their HEAD position.
# 22 markers (11 per side + center). No '_end' markers: the last
# bone of each chain is left unrotated and inherits the mannequin
# joint angle automatically.
CORE_HEAD_MARKER_BONES = (
    'pelvis', 'spine_01', 'spine_02', 'spine_03', 'neck_01', 'head',
    'clavicle_l', 'upperarm_l', 'lowerarm_l', 'hand_l',
    'thigh_l', 'calf_l', 'foot_l', 'ball_l',
    'clavicle_r', 'upperarm_r', 'lowerarm_r', 'hand_r',
    'thigh_r', 'calf_r', 'foot_r', 'ball_r',
)

# Optional thumb markers (only when "Pose Thumbs" is enabled).
THUMB_HEAD_MARKER_BONES = (
    'thumb_01_l', 'thumb_02_l', 'thumb_03_l',
    'thumb_01_r', 'thumb_02_r', 'thumb_03_r',
)

# Core bone chains for posing (always posed). The last bone of each
# chain is NOT rotated - it follows its parent with the mannequin
# joint angle.
BONE_CHAINS = (
    ['pelvis', 'spine_01', 'spine_02', 'spine_03', 'neck_01', 'head'],
    ['clavicle_l', 'upperarm_l', 'lowerarm_l', 'hand_l'],
    ['clavicle_r', 'upperarm_r', 'lowerarm_r', 'hand_r'],
    ['thigh_l', 'calf_l', 'foot_l', 'ball_l'],
    ['thigh_r', 'calf_r', 'foot_r', 'ball_r'],
)

# Thumb chains (only posed when "Pose Thumbs" option is enabled).
THUMB_CHAINS = (
    ['thumb_01_l', 'thumb_02_l', 'thumb_03_l'],
    ['thumb_01_r', 'thumb_02_r', 'thumb_03_r'],
)

# index / middle / ring / pinky - merged into hands when option is on.
# Thumbs stay articulated.
FINGER_BONE_NAMES = ('index', 'middle', 'ring', 'pinky')
FINGER_SEGMENTS = ('01', '02', '03')

# IK bones drive animation tracks in UE4 (foot/hand IK) and must NEVER
# carry skin weights: UE4 animations move those bones every frame, and
# any vertex weight on them would drag mesh parts around and corrupt
# the animation. Standard UE4 mannequin IK bones:
IK_BONE_NAME_SET = frozenset((
    'ik_foot_root', 'ik_foot_l', 'ik_foot_r',
    'ik_hand_root', 'ik_hand_gun', 'ik_hand_l', 'ik_hand_r',
))

# --------------------------------------------------------------------
# Weight masks ("vertex bind" groups)
# --------------------------------------------------------------------
# Vertex groups on the character mesh whose name starts with this
# prefix are WEIGHT MASKS, not deform groups: bones mapped to the
# group may only receive weights from the vertices INSIDE it. This
# fixes classic automatic-weight problems: arm bones stealing weights
# from the legs, weights landing on an outer layer (a bandage) while
# the layer underneath stays unweighted, etc.
#
#   vb:<part>       - a whole body part: vb:arms, vb:legs, vb:torso,
#                     vb:head, vb:hands, vb:feet (see the table below)
#   vb:<bone_name>  - a single bone override, e.g. vb:thigh_l
#
# Each body part gets its own bone-heat pass, so its bones never
# influence vertices outside the mask.
VB_PREFIX = "vb:"

# Standard UE4 body parts -> bone name substrings (lowercase match).
# Twist bones (upperarm_twist_01_l, thigh_twist_01_l, ...) fall into
# the same parts automatically.
PART_BONE_PATTERNS = (
    ('head',  ('head', 'neck')),
    ('torso', ('pelvis', 'spine')),
    ('arms',  ('clavicle', 'upperarm', 'lowerarm')),
    ('hands', ('hand', 'thumb', 'index', 'middle', 'ring', 'pinky')),
    ('legs',  ('thigh', 'calf', 'leg')),
    ('feet',  ('foot', 'ball', 'toe')),
)

KNOWN_PARTS = tuple(part for part, _patterns in PART_BONE_PATTERNS)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _props(context):
    return getattr(context.scene, "ue4_binder_props", None)


def _update(context):
    context.view_layer.update()


def _ensure_object_mode(context):
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass


def _reset_pose(context, arm):
    """Reset all pose transforms (location, scale, rotation in any mode)
    and restore bone connections that were detached by the posing step.

    Safe for FBX-imported skeletons that use quaternion rotation mode.
    """
    for pb in arm.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
        if pb.rotation_mode == 'QUATERNION':
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        else:
            pb.rotation_euler = (0.0, 0.0, 0.0)
    _update(context)
    _restore_connections(context, arm)


def _disconnect_chain_bones(context, arm, bone_names):
    """Detach the given bones from their parents' tails (use_connect
    = False) so pose mode can translate every joint independently.

    The REST GEOMETRY IS NOT CHANGED: heads keep their exact positions,
    rest transforms stay identical, so FBX export is unaffected. The
    original use_connect flags are stored on the armature (first-seen
    values win) and are restored later by the bind / reset steps.
    Returns the list of bones that got detached now.
    """
    saved = {}
    raw = arm.get("ue4_binder_saved_connect")
    if raw:
        try:
            saved = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            saved = {}

    _select_only(context, [arm], arm)
    try:
        bpy.ops.object.mode_set(mode='EDIT')
    except Exception as err:
        print("[UE4 Binder] cannot enter Edit Mode (%s) - bones stay "
              "connected to parent tails" % err)
        return []
    detached = []
    try:
        eb = arm.data.edit_bones
        for name in bone_names:
            ebone = eb.get(name)
            if ebone is None or not ebone.use_connect:
                continue
            ebone.use_connect = False
            saved.setdefault(name, True)
            detached.append(name)
        if detached:
            arm["ue4_binder_saved_connect"] = json.dumps(saved)
            print("[UE4 Binder] detached %d chain bones from parent tails "
                  "(rest geometry unchanged)" % len(detached))
    finally:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass
    return detached


def _restore_connections(context, arm):
    """Restore use_connect flags that were saved by the posing step."""
    raw = arm.get("ue4_binder_saved_connect")
    if not raw:
        return
    try:
        saved = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return
    _ensure_object_mode(context)
    _select_only(context, [arm], arm)
    try:
        bpy.ops.object.mode_set(mode='EDIT')
    except Exception as err:
        print("[UE4 Binder] cannot enter Edit Mode to restore bone "
              "connections: %s" % err)
        return
    restored = 0
    kept = []
    try:
        eb = arm.data.edit_bones
        for name, was_connected in list(saved.items()):
            ebone = eb.get(name)
            if ebone is None:
                saved.pop(name, None)
                continue
            if not was_connected:
                ebone.use_connect = False
                saved.pop(name, None)
                continue
            if ebone.use_connect:
                saved.pop(name, None)
                continue
            parent = ebone.parent
            weld_ok = (parent is None or
                       (ebone.head - parent.tail).length < 1e-5)
            if weld_ok:
                ebone.use_connect = True
                saved.pop(name, None)
                restored += 1
            else:
                # Rest geometry was edited after detaching: do not snap
                # the head onto the parent tail.
                kept.append(name)
        if kept:
            print("[UE4 Binder] %d bones left unconnected (rest geometry "
                  "changed after detaching): %s"
                  % (len(kept), ", ".join(kept)))
    finally:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass
    if restored:
        print("[UE4 Binder] restored %d bone connections" % restored)
    if saved:
        arm["ue4_binder_saved_connect"] = json.dumps(saved)
    else:
        try:
            del arm["ue4_binder_saved_connect"]
        except Exception:
            pass


def _skeleton_height(armature_obj):
    """Approximate skeleton height (world Z extent over all bones)."""
    mw = armature_obj.matrix_world
    zs = []
    for pb in armature_obj.pose.bones:
        zs.append((mw @ pb.head).z)
        zs.append((mw @ pb.tail).z)
    if not zs:
        return 0.0
    return max(zs) - min(zs)


def _mesh_world_height(mesh_obj):
    """Approximate mesh world height (Z extent of bounding box)."""
    zs = [(mesh_obj.matrix_world @ Vector(c)).z for c in mesh_obj.bound_box]
    if not zs:
        return 0.0
    return max(zs) - min(zs)


def _split_suffix(name):
    """'M_hand_l.001' -> ('M_hand_l', '.001')"""
    m = re.search(r'\.\d+$', name)
    if m:
        return name[:m.start()], m.group(0)
    return name, ""


def _marker_positions(armature_obj):
    """Return dict {base_marker_name: position in armature space} or None.

    Marker names are matched with Blender numeric suffixes stripped, so
    'M_hand_l.001' (which appears after duplicating a marker) still
    matches the bone 'hand_l'. If BOTH 'M_hand_l' and 'M_hand_l.001'
    exist, the marker FARTHER from the bone's current head wins - the
    moved copy is the intended one, while an untouched duplicate sits
    exactly at the bone head.
    """
    coll = bpy.data.collections.get(MARKER_COLLECTION_NAME)
    if coll is None:
        return None
    inv = armature_obj.matrix_world.inverted()
    grouped = {}
    for obj in coll.objects:
        base = _split_suffix(obj.name)[0]
        grouped.setdefault(base, []).append(obj)

    positions = {}
    for base, objs in grouped.items():
        if len(objs) == 1:
            positions[base] = inv @ objs[0].matrix_world.translation
            continue
        # Several markers map to the same bone name: pick the one that
        # was moved away from the bone head (a duplicate left exactly
        # at the bone head is stale).
        pb = armature_obj.pose.bones.get(base[len(MARKER_PREFIX):])
        best_obj = None
        best_pos = None
        best_dist = -1.0
        for o in objs:
            pos = inv @ o.matrix_world.translation
            if pb is not None:
                dist = (pos - pb.head).length
            elif o.name == base:
                dist = 2.0  # no bone to test: prefer the plain name
            else:
                dist = 1.0
            if dist > best_dist:
                best_dist, best_obj, best_pos = dist, o, pos
        print("[UE4 Binder] WARNING: duplicate markers for '%s' (%s) "
              "-> using '%s'" % (base, ", ".join(o.name for o in objs),
                                 best_obj.name))
        positions[base] = best_pos
    return positions


def _sagittal_mirror_plane(arm):
    """Return (point, normal) of the skeleton's left/right symmetry
    plane in ARMATURE SPACE, derived from symmetric rest bone pairs.

    This avoids assuming which local axis is 'left-right': UE4 FBX
    imports can face +X or -Y in armature local space, so mirroring
    across a fixed axis mirrors the wrong plane. Rest coordinates
    (bone.head_local) are used, so the result does not depend on the
    current pose. Returns None if no symmetric pairs exist.
    """
    pairs = (
        ('clavicle_l', 'clavicle_r'),
        ('upperarm_l', 'upperarm_r'),
        ('hand_l', 'hand_r'),
        ('thigh_l', 'thigh_r'),
        ('foot_l', 'foot_r'),
    )
    mids = []
    normals = []
    ref_normal = None
    for lname, rname in pairs:
        pl = arm.pose.bones.get(lname)
        pr = arm.pose.bones.get(rname)
        if pl is None or pr is None:
            continue
        hl = pl.bone.head_local.copy()
        hr = pr.bone.head_local.copy()
        d = hr - hl
        if d.length < 1e-9:
            continue
        d.normalize()
        # Align all normals to the same half-space before averaging.
        if ref_normal is None:
            ref_normal = d.copy()
        elif d.dot(ref_normal) < 0.0:
            d = -d
        normals.append(d)
        mids.append((hl + hr) * 0.5)
    if not normals:
        return None
    normal = Vector()
    for n in normals:
        normal += n
    if normal.length < 1e-9:
        return None
    normal.normalize()
    point = mids[0]
    print("[UE4 Binder] mirror plane: point=%s normal=%s (from %d bone pairs)"
          % (tuple(round(c, 3) for c in point),
             tuple(round(c, 3) for c in normal), len(normals)))
    return point, normal


def _reflect_across_plane(pos, point, normal):
    """Mirror position across the plane defined by (point, normal)."""
    return pos - normal * (2.0 * (pos - point).dot(normal))


def _select_only(context, objects, active):
    """Deselect everything, select given objects, set active."""
    _ensure_object_mode(context)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in objects:
        try:
            obj.select_set(True)
        except Exception:
            pass
    if active is not None:
        context.view_layer.objects.active = active


def _is_ik_bone(name):
    """True for IK bones (ik_foot_l, ik_hand_root, foot_ik_l, ...).

    IK bones drive animation in UE4 and must never receive skin
    weights, otherwise the animation moves the mesh incorrectly."""
    low = name.lower()
    if low in IK_BONE_NAME_SET:
        return True
    return "ik" in low.split("_")


def _remove_ik_vertex_groups(mesh_obj):
    """Delete vertex groups that belong to IK bones (ik_*). Returns the
    number of removed groups."""
    removed = 0
    for vg in list(mesh_obj.vertex_groups):
        if _is_ik_bone(vg.name):
            mesh_obj.vertex_groups.remove(vg)
            removed += 1
    return removed


def _merge_finger_weights(mesh_obj):
    """Merge index/middle/ring/pinky weights into hand_l / hand_r,
    then delete the finger vertex groups. Returns number of groups
    merged."""
    finger_names = set()
    for side in ('l', 'r'):
        for finger in FINGER_BONE_NAMES:
            for seg in FINGER_SEGMENTS:
                finger_names.add("%s_%s_%s" % (finger, seg, side))

    hand_groups = {
        'l': mesh_obj.vertex_groups.get('hand_l'),
        'r': mesh_obj.vertex_groups.get('hand_r'),
    }

    merged = 0
    to_remove = []
    for vg in mesh_obj.vertex_groups:
        if vg.name not in finger_names:
            continue
        side = vg.name[-1]  # group names end with '_l' / '_r'
        hand = hand_groups.get(side)
        if hand is None:
            continue
        added = 0
        for vert in mesh_obj.data.vertices:
            for g in vert.groups:
                if g.group == vg.index and g.weight > 0.0:
                    hand.add([vert.index], g.weight, 'ADD')
                    added += 1
        if added:
            to_remove.append(vg)
            merged += 1
    for vg in to_remove:
        mesh_obj.vertex_groups.remove(vg)
    return merged


def _cleanup_weights(context, mesh_obj):
    """Normalize, clean near-zero and limit to 4 influences per vertex."""
    _ensure_object_mode(context)
    context.view_layer.objects.active = mesh_obj
    try:
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        try:
            bpy.ops.object.vertex_group_normalize_all(
                group_select_mode='ALL', lock_active=False)
        except Exception:
            pass
        try:
            bpy.ops.object.vertex_group_clean(
                group_select_mode='ALL', limit=0.005)
        except Exception:
            pass
        try:
            bpy.ops.object.vertex_group_limit_total(
                group_select_mode='ALL', limit=4)
        except Exception:
            pass
        # limit_total drops the small influences AFTER normalization,
        # which leaves the vertex sums below 1.0 - renormalize once more.
        try:
            bpy.ops.object.vertex_group_normalize_all(
                group_select_mode='ALL', lock_active=False)
        except Exception:
            pass
    finally:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass


def _cleanup_deform_weights(mesh_obj, deform_names):
    """Normalize deform weights per vertex, keep the 4 best influences,
    zero the rest. Unlike the operator-based cleanup this NEVER touches
    the vb: mask groups (they are not deform groups)."""
    groups = mesh_obj.vertex_groups
    deform_set = set(deform_names)
    deform_idx = {vg.index for vg in groups if vg.name in deform_set}
    if not deform_idx:
        return
    for vert in mesh_obj.data.vertices:
        entries = [(g.group, g.weight) for g in vert.groups
                   if g.group in deform_idx and g.weight > 0.0]
        if not entries:
            continue
        entries.sort(key=lambda item: -item[1])
        top = [e for e in entries if e[1] > 0.005][:4]
        if not top:
            # everything was near zero - keep the single best influence
            top = entries[:1]
        top_ids = {gi for gi, _w in top}
        total = sum(w for _gi, w in top)
        if total < 1e-9:
            continue
        for gi, w in top:
            groups[gi].add([vert.index], w / total, 'REPLACE')
        for gi, _w in entries:
            if gi not in top_ids:
                groups[gi].add([vert.index], 0.0, 'REPLACE')


# --------------------------------------------------------------------
# Weight mask helpers
# --------------------------------------------------------------------

def _part_of_bone(bone_name):
    """Map a UE4 bone name to its body part ('arms', 'legs', ...) or None."""
    low = bone_name.lower()
    if _is_ik_bone(low):
        return None  # ik_foot_l, ik_hand_l, ... are not deform parts
    for part, patterns in PART_BONE_PATTERNS:
        for pattern in patterns:
            if pattern in low:
                return part
    return None


def _verts_of_group(mesh_obj, vg_name):
    """Vertex indices belonging to a vertex group."""
    vg = mesh_obj.vertex_groups.get(vg_name)
    if vg is None:
        return []
    return [v.index for v in mesh_obj.data.vertices
            for g in v.groups if g.group == vg.index]


def _point_segment_dist(p, a, b):
    """Distance from point p to segment a-b (mathutils.Vector)."""
    ab = b - a
    denom = ab.length_squared
    t = 0.0 if denom < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / denom))
    return (p - (a + ab * t)).length


def _remove_object(obj):
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass


def _auto_weights_pass(context, mesh_obj, arm_obj, bone_names, vert_indices):
    """Bone-heat automatic weights for ONLY the given bones, computed on
    ONLY the given vertices, then copied back into the real mesh.

    Implementation: a TEMP armature (bones baked at their CURRENT POSED
    positions - that is the whole point of the temporary-pose binding)
    and a TEMP mesh copy with all other vertices deleted are bound with
    the standard ARMATURE_AUTO operator; the resulting weights are
    copied back by vertex index. The real mesh/armature are untouched
    until the caller parents the mesh itself.
    """
    if not bone_names or not vert_indices:
        return 0
    keep = set(vert_indices)
    scene = context.scene

    tmp_arm = None
    tmp_arm_data = None
    tmp_obj = None
    tmp_mesh = None
    try:
        # --- temp armature with only the pass bones -----------------
        tmp_arm_data = bpy.data.armatures.new("UE4BINDER_TMP_ARM_DATA")
        tmp_arm = bpy.data.objects.new("UE4BINDER_TMP_ARM", tmp_arm_data)
        tmp_arm.matrix_world = arm_obj.matrix_world.copy()
        scene.collection.objects.link(tmp_arm)

        mw = arm_obj.matrix_world
        inv = mw.inverted()
        _select_only(context, [tmp_arm], tmp_arm)
        bpy.ops.object.mode_set(mode='EDIT')
        edit_bones = tmp_arm.data.edit_bones
        for name in bone_names:
            pb = arm_obj.pose.bones.get(name)
            rb = arm_obj.data.bones.get(name)
            if pb is None or rb is None:
                continue
            eb = edit_bones.new(name)
            # Bake the CURRENT (posed) joint positions into the temp
            # rest geometry so the heat solver sees the bent pose.
            eb.head = inv @ (mw @ pb.head)
            eb.tail = inv @ (mw @ pb.tail)
            eb.envelope_distance = rb.envelope_distance
            eb.envelope_weight = rb.envelope_weight
            eb.use_deform = True
        bpy.ops.object.mode_set(mode='OBJECT')
        _update(context)

        # --- temp mesh with only the pass vertices ------------------
        tmp_mesh = mesh_obj.data.copy()
        tmp_obj = bpy.data.objects.new("UE4BINDER_TMP_MESH", tmp_mesh)
        # Stale vertex-group memberships (e.g. the vb: masks copied with
        # the data) point at vertices that are about to be deleted and
        # make the ARMATURE_AUTO operator spam "Vertex not in group".
        # The pass does not need them - drop them all.
        tmp_obj.vertex_groups.clear()
        # Remember the ORIGINAL vertex index on every temp vertex: the
        # mapping back must stay exact even though the deletion below
        # renumbers the surviving vertices.
        idx_attr = tmp_mesh.attributes.get("ue4_binder_orig_idx")
        if idx_attr is None:
            idx_attr = tmp_mesh.attributes.new(
                "ue4_binder_orig_idx", 'INT', 'POINT')
        idx_attr.data.foreach_set(
            "value", list(range(len(tmp_mesh.vertices))))
        tmp_obj.matrix_world = mesh_obj.matrix_world.copy()
        scene.collection.objects.link(tmp_obj)

        bm = bmesh.new()
        bm.from_mesh(tmp_mesh)
        bm.verts.ensure_lookup_table()
        to_delete = []
        kept = []  # original indices, ascending (bmesh keeps order)
        for v in bm.verts:
            if v.index in keep:
                kept.append(v.index)
            else:
                to_delete.append(v)
        for v in to_delete:
            bm.verts.remove(v)
        bm.to_mesh(tmp_mesh)
        bm.free()
        if len(tmp_mesh.vertices) != len(kept):
            raise RuntimeError("temp mesh vertex count mismatch")
        # Exact original-index mapping, read back from the attribute
        # (vertex indices were renumbered by the deletion).
        orig_of_tmp = [0] * len(tmp_mesh.vertices)
        (tmp_mesh.attributes.get("ue4_binder_orig_idx").data
         .foreach_get("value", orig_of_tmp))
        idx_map = [(orig_of_tmp[i], i) for i in range(len(orig_of_tmp))]

        # --- classic auto-weights recipe on the temp pair -----------
        _ensure_object_mode(context)
        bpy.ops.object.select_all(action='DESELECT')
        tmp_obj.select_set(True)
        tmp_arm.select_set(False)
        context.view_layer.objects.active = tmp_arm
        result = bpy.ops.object.parent_set(type='ARMATURE_AUTO')
        if 'FINISHED' not in result:
            raise RuntimeError("Bone Heat solver failed on the subset "
                               "(%d bones, %d vertices)"
                               % (len(bone_names), len(kept)))

        # --- copy the weights back into the real mesh ---------------
        copied = 0
        for name in bone_names:
            src = tmp_obj.vertex_groups.get(name)
            if src is None:
                continue
            dst = mesh_obj.vertex_groups.get(name)
            if dst is None:
                dst = mesh_obj.vertex_groups.new(name=name)
            for orig_i, tmp_i in idx_map:
                try:
                    w = src.weight(tmp_i)
                except RuntimeError:
                    continue  # vertex has no weight for this bone
                if w > 1e-5:
                    dst.add([orig_i], w, 'REPLACE')
                    copied += 1
        return copied
    finally:
        _ensure_object_mode(context)
        if tmp_obj is not None:
            _remove_object(tmp_obj)
        if tmp_arm is not None:
            _remove_object(tmp_arm)
        # Purge the orphaned datablocks too, otherwise a full copy of
        # the character mesh (and the temp armature) silently stays in
        # the .blend file.
        if tmp_mesh is not None:
            try:
                bpy.data.meshes.remove(tmp_mesh)
            except Exception:
                pass
        if tmp_arm_data is not None:
            try:
                bpy.data.armatures.remove(tmp_arm_data)
            except Exception:
                pass
        _update(context)


def _distance_weights_pass(mesh_obj, arm_obj, bone_names, vert_indices):
    """Fallback when the Bone Heat solver fails on a subset (enclosed
    layers, self-intersections): assign weights by inverse-square
    distance to the CURRENT (posed) bone segments, in mesh-local space.
    Coarse but always succeeds and keeps each bone inside its own
    subset, so masks still cannot leak."""
    if not bone_names or not vert_indices:
        return 0
    mw = arm_obj.matrix_world
    inv = mesh_obj.matrix_world.inverted()
    segs = []
    max_len = 1e-6
    for name in bone_names:
        pb = arm_obj.pose.bones.get(name)
        if pb is None:
            continue
        a = inv @ (mw @ pb.head)
        b = inv @ (mw @ pb.tail)
        segs.append((name, a, b))
        max_len = max(max_len, (b - a).length)
    if not segs:
        return 0
    eps = (0.001 * max_len) ** 2  # keeps 1/d^2 finite on the bone itself
    written = 0
    for vi in vert_indices:
        p = mesh_obj.data.vertices[vi].co
        raw = []
        for name, a, b in segs:
            d = _point_segment_dist(p, a, b)
            raw.append((name, 1.0 / (d * d + eps)))
        total = sum(w for _n, w in raw)
        if total <= 1e-12:
            continue
        for name, w in raw:
            w /= total
            if w < 1e-4:
                continue
            dst = mesh_obj.vertex_groups.get(name)
            if dst is None:
                dst = mesh_obj.vertex_groups.new(name=name)
            dst.add([vi], w, 'REPLACE')
            written += 1
    return written


def _repair_zero_weights(mesh_obj):
    """Give zero-weight vertices the weights of the nearest weighted
    vertex (KD-tree over the mesh). Fixes the classic multi-layer
    problem: the layer hidden under a bandage/shell stays unweighted
    after bone-heat because heat cannot reach it.

    Vertices that belong to vb: mask groups only receive weights from
    vertices of the SAME mask group - the mask never leaks across
    body parts. Returns (repaired, unresolved) counts.
    """
    me = mesh_obj.data
    n = len(me.vertices)
    groups = mesh_obj.vertex_groups
    vg_names = [vg.name for vg in groups]
    vb_idx = {vg.index: vg.name for vg in groups
              if vg.name.startswith(VB_PREFIX)}

    # current weights per vertex (deform groups only)
    weights = [None] * n
    member_sets = [None] * n
    for vert in me.vertices:
        d = {}
        vb_set = set()
        for g in vert.groups:
            gname = vg_names[g.group]
            if gname in vb_idx.values():
                vb_set.add(gname)
            elif g.weight > 1e-5:
                d[gname] = g.weight
        if d:
            weights[vert.index] = d
        if vb_set:
            member_sets[vert.index] = frozenset(vb_set)

    src_all = [i for i in range(n) if weights[i]]
    if not src_all:
        return 0, n

    common_kd = KDTree(len(src_all))
    for i in src_all:
        common_kd.insert(me.vertices[i].co, i)
    common_kd.balance()

    # one KD-tree per vb: group (only vertices of that group are
    # allowed as sources for its members)
    group_kds = {}
    for gname in sorted(set(vb_idx.values())):
        srcs = [i for i in src_all if gname in (member_sets[i] or frozenset())]
        if not srcs:
            continue
        kd = KDTree(len(srcs))
        for i in srcs:
            kd.insert(me.vertices[i].co, i)
        kd.balance()
        group_kds[gname] = kd

    repaired = 0
    unresolved = 0
    for i in range(n):
        if weights[i]:
            continue
        best_i = None
        best_d = None
        mem = member_sets[i]
        if mem:
            for gname in mem:
                kd = group_kds.get(gname)
                if kd is None:
                    continue
                _co, idx, dist = kd.find(me.vertices[i].co)
                if idx is not None and (best_d is None or dist < best_d):
                    best_d, best_i = dist, idx
        else:
            _co, idx, dist = common_kd.find(me.vertices[i].co)
            if idx is not None:
                best_i = idx
        if best_i is None:
            unresolved += 1
            continue
        for name, w in weights[best_i].items():
            dst = groups.get(name)
            if dst is None:
                dst = groups.new(name=name)
            dst.add([i], w, 'REPLACE')
        repaired += 1
    return repaired, unresolved


# --------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------

class UE4BinderProperties(PropertyGroup):
    target_armature: PointerProperty(
        name="UE4 Armature",
        type=bpy.types.Object,
        description="Standard UE4 skeleton (from mannequin FBX). Its rest pose stays untouched"
    )
    target_mesh: PointerProperty(
        name="Character Mesh",
        type=bpy.types.Object,
        description="Your custom character mesh (no skeleton, A-pose)"
    )
    pose_thumbs: BoolProperty(
        name="Pose Thumbs With Markers",
        description="Create markers for thumb chains and pose them. OFF = thumbs just follow the posed hand (fewer markers)",
        default=False
    )
    merge_fingers: BoolProperty(
        name="Merge Fingers Into Hands",
        description="Transfer index/middle/ring/pinky weights into hand_l/hand_r and delete finger groups (thumbs stay articulated)",
        default=True
    )
    reset_after_bind: BoolProperty(
        name="Reset Pose After Bind",
        description="Reset skeleton to standard rest pose right after binding (recommended ON)",
        default=True
    )


# --------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------

class OBJECT_OT_ue4_check_setup(Operator):
    bl_idname = "object.ue4_check_setup"
    bl_label = "1. Check Setup"
    bl_description = "Validate armature (UE4 bone names), mesh state, transforms and height ratio"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_armature is not None

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        mesh = props.target_mesh

        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Target armature is not set or is not an armature")
            return {'CANCELLED'}

        notes = []
        missing = [b for b in REQUIRED_BONES if arm.pose.bones.get(b) is None]
        if 'pelvis' in missing:
            self.report({'ERROR'}, "This is not a UE4 mannequin skeleton: 'pelvis' bone not found")
            return {'CANCELLED'}
        if missing:
            notes.append("missing bones: " + ", ".join(missing))
        else:
            notes.append("UE4 skeleton bones: OK (%d pose bones)" % len(arm.pose.bones))

        if not mesh or mesh.type != 'MESH':
            self.report({'WARNING'}, "Skeleton OK, but character mesh is not set: " + "; ".join(notes))
            return {'FINISHED'}

        if mesh.parent is arm:
            notes.append("WARNING: mesh is already parented to this armature (unbind before re-binding)")
        if mesh.vertex_groups:
            notes.append("WARNING: mesh already has %d vertex groups" % len(mesh.vertex_groups))

        rot = mesh.matrix_world.to_quaternion()
        scl = mesh.matrix_world.to_scale()
        rot_dirty = (rot.w < 0.99999 or abs(rot.x) > 1e-5 or abs(rot.y) > 1e-5 or abs(rot.z) > 1e-5)
        scl_dirty = any(abs(s - 1.0) > 1e-5 for s in scl)
        if rot_dirty or scl_dirty:
            notes.append("mesh rotation/scale will be auto-applied on Bind")

        sh = _skeleton_height(arm)
        mh = _mesh_world_height(mesh)
        if sh > 1e-6 and mh > 1e-6:
            ratio = sh / mh
            notes.append("heights: skeleton=%.3f mesh=%.3f -> suggested mesh scale factor=%.3f" % (sh, mh, ratio))

        for line in notes:
            print("[UE4 Binder]", line)

        self.report({'INFO'}, "Check finished: %s" % " | ".join(notes))
        return {'FINISHED'}


class OBJECT_OT_ue4_fit_height(Operator):
    bl_idname = "object.ue4_fit_height"
    bl_label = "1b. Fit Mesh Height to Skeleton (optional)"
    bl_description = "Scale character mesh so its bounding box height matches skeleton height (rough helper)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return (props is not None and props.target_armature is not None
                and props.target_mesh is not None)

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        mesh = props.target_mesh
        if not arm or arm.type != 'ARMATURE' or not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid armature and mesh first")
            return {'CANCELLED'}

        sh = _skeleton_height(arm)
        mh = _mesh_world_height(mesh)
        if sh <= 1e-6 or mh <= 1e-6:
            self.report({'ERROR'}, "Could not measure heights (zero bounding box?)")
            return {'CANCELLED'}

        factor = sh / mh
        mesh.scale = tuple(c * factor for c in mesh.scale)
        _update(context)
        self.report({'INFO'}, "Mesh scaled by %.3f (mesh %.3f -> skeleton %.3f)" % (factor, mh, sh))
        return {'FINISHED'}


class OBJECT_OT_ue4_create_markers(Operator):
    bl_idname = "object.ue4_create_markers"
    bl_label = "2. Create Joint Markers"
    bl_description = "Create empty markers at skeleton joint positions. Then MOVE them onto your character's joints"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_armature is not None

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Set a valid armature first")
            return {'CANCELLED'}

        # Make sure markers are created at the STANDARD rest pose.
        _reset_pose(context, arm)
        _update(context)

        # Clean previous collection.
        old = bpy.data.collections.get(MARKER_COLLECTION_NAME)
        if old is not None:
            for obj in list(old.objects):
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass
            bpy.data.collections.remove(old)

        coll = bpy.data.collections.new(MARKER_COLLECTION_NAME)
        context.scene.collection.children.link(coll)

        size = max(0.02, _skeleton_height(arm) * 0.025)
        mw = arm.matrix_world
        created = 0

        def new_marker(name, position_world):
            nonlocal created
            marker = bpy.data.objects.new(name, None)
            marker.empty_display_type = 'SPHERE'
            marker.empty_display_size = size
            marker.show_name = True  # display the name near the marker
            marker.location = position_world
            coll.objects.link(marker)
            created += 1

        marker_bones = list(CORE_HEAD_MARKER_BONES)
        if props.pose_thumbs:
            marker_bones += list(THUMB_HEAD_MARKER_BONES)

        for bone_name in marker_bones:
            pb = arm.pose.bones.get(bone_name)
            if pb is None:
                print("[UE4 Binder] no bone '%s', marker skipped" % bone_name)
                continue
            new_marker(MARKER_PREFIX + bone_name, mw @ pb.head)

        _update(context)
        self.report({'INFO'}, "Created %d markers. Now move them onto your character's joints" % created)
        return {'FINISHED'}


class OBJECT_OT_ue4_mirror_markers(Operator):
    bl_idname = "object.ue4_mirror_markers"
    bl_label = "3. Mirror Markers (L -> R)"
    bl_description = ("Copy left marker positions to the right side, mirrored "
                      "across the skeleton's symmetry plane (derived from the "
                      "bones themselves - works for any FBX orientation). "
                      "Selection and editor mode do not matter. Missing "
                      "right-side markers are created automatically")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_armature is not None

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Target armature is not set (step 1)")
            return {'CANCELLED'}

        coll = bpy.data.collections.get(MARKER_COLLECTION_NAME)
        if coll is None or not coll.objects:
            self.report({'ERROR'},
                        "No markers found. Run step 2 (Create Joint Markers) first")
            return {'CANCELLED'}

        _ensure_object_mode(context)

        # Mirror across the skeleton's own symmetry plane: correct
        # even when the armature's local axes are rotated (UE4 FBX
        # imports often face +X in local space).
        plane = _sagittal_mirror_plane(arm)
        if plane is None:
            self.report({'ERROR'},
                        "Cannot derive the mirror plane: no symmetric bone "
                        "pairs found (clavicle_l/r, thigh_l/r, ...)")
            return {'CANCELLED'}
        plane_point, plane_normal = plane

        arm_mw = arm.matrix_world
        arm_inv = arm_mw.inverted()

        mirrored = 0
        created = 0
        left_names = []

        for obj in list(coll.objects):
            base, suffix = _split_suffix(obj.name)
            low = base.lower()
            if low.endswith('_l'):
                right_base = base[:-2] + '_r'
            elif low.endswith('.l'):
                right_base = base[:-2] + '.r'
            else:
                continue  # center or right marker - skip
            left_names.append(obj.name)

            left_local = arm_inv @ obj.matrix_world.translation
            right_local = _reflect_across_plane(left_local, plane_point,
                                                plane_normal)
            right_world = arm_mw @ right_local

            right = coll.objects.get(right_base + suffix)
            if right is None:
                right = coll.objects.get(right_base)

            if right is not None:
                mw = right.matrix_world.copy()
                mw.translation = right_world
                right.matrix_world = mw
                mirrored += 1
            else:
                # Right twin was deleted (or never created): make it.
                marker = bpy.data.objects.new(right_base + suffix, None)
                marker.empty_display_type = obj.empty_display_type
                marker.empty_display_size = obj.empty_display_size
                marker.show_name = True
                marker.location = right_world
                coll.objects.link(marker)
                created += 1

        if not left_names:
            print("[UE4 Binder] mirror: markers in '%s': %s" % (
                MARKER_COLLECTION_NAME,
                ", ".join(o.name for o in coll.objects) or "(none)"))
            self.report({'ERROR'},
                        "No left markers found (names ending with '_l'). "
                        "Run step 2 first - see System Console for details")
            return {'CANCELLED'}

        msg = "Mirrored %d markers" % mirrored
        if created:
            msg += ", created %d missing right markers" % created
        self.report({'INFO'}, msg)
        print("[UE4 Binder] mirror: mirrored=%d created=%d left=%s" % (
            mirrored, created, ", ".join(left_names)))
        return {'FINISHED'}


class OBJECT_OT_ue4_pose_to_markers(Operator):
    bl_idname = "object.ue4_pose_to_markers"
    bl_label = "4. Pose Skeleton to Markers"
    bl_description = ("Temporarily pose the skeleton: EVERY joint snaps "
                      "exactly onto its marker (bones rotate and stretch "
                      "in pose mode). Rest pose and real bone lengths are "
                      "NOT modified - reset restores everything")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_armature is not None

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Set a valid armature first")
            return {'CANCELLED'}

        markers = _marker_positions(arm)
        if markers is None:
            self.report({'ERROR'}, "No marker collection. Run step 2 first")
            return {'CANCELLED'}

        _ensure_object_mode(context)

        # Always start from the standard rest pose.
        _reset_pose(context, arm)
        _update(context)

        chains = list(BONE_CHAINS)
        if props.pose_thumbs:
            chains += list(THUMB_CHAINS)

        # --- Diagnostics: which chain bones found a marker? --------
        chain_bones_all = set()
        for chain_bones in chains:
            chain_bones_all.update(
                b for b in chain_bones if b in arm.pose.bones)
        matched = sorted(
            b for b in chain_bones_all if (MARKER_PREFIX + b) in markers)
        missing = sorted(chain_bones_all - set(matched))
        print("[UE4 Binder] markers in collection: %d" % len(markers))
        print("[UE4 Binder] markers matched to chain bones: %d/%d"
              % (len(matched), len(chain_bones_all)))
        if missing:
            print("[UE4 Binder] NO marker found for: %s" % ", ".join(missing))
            print("[UE4 Binder] markers present: %s"
                  % ", ".join(sorted(markers)))
        if not matched:
            self.report({'ERROR'},
                        "No markers match the skeleton bones (names must be "
                        "like 'M_spine_01'). See System Console for details")
            return {'CANCELLED'}

        print("[UE4 Binder] armature object: loc=%s rot=%s scale=%s"
              % (tuple(round(c, 3) for c in arm.location),
                 tuple(round(c, 3) for c in
                       arm.matrix_world.to_euler()),
                 tuple(round(c, 3) for c in arm.matrix_world.to_scale())))

        # If every marker sits exactly on a rest-pose bone head there
        # is nothing to pose - say so instead of silently changing
        # nothing.
        pre_max = 0.0
        pre_name = ""
        for bone_name in matched:
            pb = arm.pose.bones[bone_name]
            d = (pb.head - markers[MARKER_PREFIX + bone_name]).length
            if d > pre_max:
                pre_max, pre_name = d, bone_name
        if pre_max < 1e-4:
            print("[UE4 Binder] all markers are ON the skeleton joints "
                  "(max offset %.4f) - nothing to pose" % pre_max)
            self.report({'WARNING'},
                        "Markers are still at the skeleton joints - nothing "
                        "to pose. Move them onto your character first "
                        "(step 2 resets markers, place them again)")
            return {'FINISHED'}
        print("[UE4 Binder] farthest marker from rest joint: %s (%.3f)"
              % (pre_name, pre_max))

        # --- Marker sanity check: how far is each marker from the ---
        # --- character mesh surface? Floating markers (wrong depth,
        # --- wrong scale) are the most common cause of a bad pose.
        mesh = props.target_mesh
        if mesh is not None and mesh.type == 'MESH':
            try:
                dg = context.evaluated_depsgraph_get()
                tree = BVHTree.FromObject(mesh, dg)
                mesh_inv = mesh.matrix_world.inverted()
                arm_mw = arm.matrix_world
                thr = max(1e-6, _skeleton_height(arm) * 0.12)
                bad = []
                for bone_name in matched:
                    pos_world = arm_mw @ markers[MARKER_PREFIX + bone_name]
                    hit = tree.find_nearest(mesh_inv @ pos_world)
                    dist = hit[3] if (hit and hit[0] is not None) else None
                    if dist is None or dist > thr:
                        bad.append((MARKER_PREFIX + bone_name, dist))
                if bad:
                    print("[UE4 Binder] MARKER CHECK - markers far from the "
                          "character mesh surface (threshold %.2f):" % thr)
                    for name, dist in sorted(bad,
                                             key=lambda kv: -(kv[1] or 0)):
                        if dist is None:
                            print("   %s: no surface found" % name)
                        else:
                            print("   %s: %.2f" % (name, dist))
                    print("[UE4 Binder] -> move these markers ONTO the mesh "
                          "joints (check depth in side view / scale)")
            except Exception as err:
                print("[UE4 Binder] marker surface check skipped: %s" % err)

        # Detach chain bones from their parent tails so every joint can
        # be placed independently in pose mode. Rest geometry is NOT
        # changed; the flags are restored by the bind/reset steps.
        chain_names = [b for chain in chains
                       for b in chain if b in arm.pose.bones]
        _disconnect_chain_bones(context, arm, chain_names)
        still_welded = [n for n in chain_names
                        if arm.pose.bones[n].bone.use_connect]
        if still_welded:
            print("[UE4 Binder] NOTE: %d bones stay welded to parent tails "
                  "- their joints cannot be snapped: %s"
                  % (len(still_welded), ", ".join(still_welded)))

        # --- Pose: every joint snaps EXACTLY onto its marker. ------
        # Each bone: 1) translate the head onto its own marker,
        # 2) rotate around the head and STRETCH (UNIFORM pose scale -
        # never shears the hierarchy) so the tail lands on the next
        # bone's marker. The last bone of a chain keeps the mannequin
        # joint angle: only its head snaps onto its marker.
        for chain_bones in chains:
            chain = [b for b in chain_bones if b in arm.pose.bones]
            for i, bone_name in enumerate(chain):
                pb = arm.pose.bones[bone_name]
                _update(context)

                # 1) head -> own marker
                marker = markers.get(MARKER_PREFIX + bone_name)
                if marker is not None:
                    off = marker - pb.head
                    if off.length > 1e-6:
                        pb.matrix = Matrix.Translation(off) @ pb.matrix
                        _update(context)

                # 2) tail -> next bone's marker
                if i >= len(chain) - 1:
                    continue
                nxt = markers.get(MARKER_PREFIX + chain[i + 1])
                if nxt is None:
                    continue
                head = pb.head.copy()
                d = nxt - head
                cur_len = (pb.tail - head).length
                if d.length < 1e-9 or cur_len < 1e-9:
                    continue
                cur_dir = (pb.tail - head).normalized()
                rot = cur_dir.rotation_difference(d.normalized())
                s = d.length / cur_len
                pivot = Matrix.Translation(head)
                pb.matrix = (pivot @ rot.to_matrix().to_4x4()
                             @ pivot.inverted() @ pb.matrix
                             @ Matrix.Diagonal((s, s, s, 1.0)))
                _update(context)

        _update(context)

        # Diagnostics: head-to-marker errors (should be ~0 for ALL
        # bones now - any non-zero value means the joint could not be
        # placed, e.g. edit mode was unavailable).
        deviations = {}
        for bone_name in chain_names:
            marker = markers.get(MARKER_PREFIX + bone_name)
            if marker is not None:
                deviations[bone_name] = (
                    arm.pose.bones[bone_name].head - marker).length
        worst = [kv for kv in deviations.items() if kv[1] > 0.01]
        summary = ("Skeleton posed: every joint snapped to its marker, "
                   "bones stretched to fit (pose only)")
        if worst:
            worst.sort(key=lambda kv: kv[1], reverse=True)
            text = ", ".join("%s: %.3f" % (n, v) for n, v in worst[:5])
            print("[UE4 Binder] joints NOT exactly on marker (should be "
                  "~0):", text)
            summary += ". Not snapped: " + text
        if missing:
            summary += ". NO marker for: " + ", ".join(missing)
        self.report({'INFO'}, summary)
        return {'FINISHED'}


class OBJECT_OT_ue4_bind(Operator):
    bl_idname = "object.ue4_bind"
    bl_label = "5. Bind (Auto Weights)"
    bl_description = "Parent mesh to armature with automatic weights in the current (bent) pose, optionally merge fingers, then reset skeleton to standard pose"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return (props is not None and props.target_armature is not None
                and props.target_mesh is not None)

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        mesh = props.target_mesh

        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Set a valid armature first")
            return {'CANCELLED'}
        if not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid mesh first")
            return {'CANCELLED'}
        if mesh.parent is arm:
            self.report({'ERROR'}, "Mesh is already parented to this armature. Unbind first (Alt+P > Clear Parent and Keep Transform)")
            return {'CANCELLED'}

        _ensure_object_mode(context)

        # Apply mesh object rotation/scale (required for correct
        # heat weights and clean FBX export).
        _select_only(context, [mesh], mesh)
        rot = mesh.matrix_world.to_quaternion()
        scl = mesh.matrix_world.to_scale()
        rot_dirty = (rot.w < 0.99999 or abs(rot.x) > 1e-5 or abs(rot.y) > 1e-5 or abs(rot.z) > 1e-5)
        scl_dirty = any(abs(s - 1.0) > 1e-5 for s in scl)
        if rot_dirty or scl_dirty:
            try:
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            except Exception as err:
                self.report({'WARNING'}, "Could not apply mesh transforms: %s" % err)

        # IK bones (ik_foot_l, ik_hand_root, ...) drive animation in UE4
        # and must stay weight-free. Temporarily flag them non-deform so
        # the Bone Heat solver cannot see them, and wipe stale IK vertex
        # groups left over from earlier binds.
        ik_bones = [b for b in arm.data.bones
                    if b.use_deform and _is_ik_bone(b.name)]
        ik_groups_cleaned = _remove_ik_vertex_groups(mesh)
        for b in ik_bones:
            b.use_deform = False
        _update(context)

        # Classic auto-weights recipe: the MESH is selected, the
        # ARMATURE is the ACTIVE object. With the mesh active this
        # operator silently does NOTHING (no parent, no weights), and
        # with both selected+armature active it can hit a
        # "Loop in parents" error, so the armature stays unselected.
        bpy.ops.object.select_all(action='DESELECT')
        try:
            mesh.select_set(True)
        except Exception:
            pass
        try:
            arm.select_set(False)
        except Exception:
            pass
        context.view_layer.objects.active = arm
        try:
            result = bpy.ops.object.parent_set(type='ARMATURE_AUTO')
        except RuntimeError as err:
            result = None
            err_msg = str(err)
        finally:
            for b in ik_bones:
                b.use_deform = True
            _update(context)

        if result is None:
            self.report({'ERROR'}, "Automatic weights failed: %s. Check for holes/disconnected mesh parts, or use envelope binding" % err_msg)
            return {'CANCELLED'}

        if 'FINISHED' not in result:
            self.report({'ERROR'}, "Automatic weights failed (Bone Heat solver). Fix mesh holes / intersections and retry")
            return {'CANCELLED'}

        # Safety net: the heat solver must not have created IK groups.
        ik_groups_cleaned += _remove_ik_vertex_groups(mesh)

        if mesh.parent is not arm or not mesh.vertex_groups:
            self.report({'ERROR'},
                        "Automatic weights produced no vertex groups. "
                        "Check that the mesh is visible/selected and "
                        "retry")
            return {'CANCELLED'}

        merged = 0
        if props.merge_fingers:
            merged = _merge_finger_weights(mesh)

        _cleanup_weights(context, mesh)

        if props.reset_after_bind:
            _reset_pose(context, arm)
            _update(context)

        message = "Bound with auto weights"
        if ik_bones:
            message += ", %d IK bones excluded" % len(ik_bones)
        if ik_groups_cleaned:
            message += ", %d IK groups cleaned" % ik_groups_cleaned
        if merged:
            message += ", %d finger groups merged" % merged
        if props.reset_after_bind:
            message += ". Pose reset: skeleton standard, mesh back to authored shape"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class OBJECT_OT_ue4_auto_split_parts(Operator):
    bl_idname = "object.ue4_auto_split_parts"
    bl_label = "5a. Auto Split Parts (Nearest Bone)"
    bl_description = ("Create vb:<part> weight-mask groups (vb:arms, "
                      "vb:legs, vb:torso, vb:head, vb:hands, vb:feet) by "
                      "assigning every mesh vertex to the part of its "
                      "NEAREST deform bone (rest pose). Adjust the groups "
                      "by hand afterwards if needed, then run 5b")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return (props is not None and props.target_armature is not None
                and props.target_mesh is not None)

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        mesh = props.target_mesh
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Set a valid armature first")
            return {'CANCELLED'}
        if not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid mesh first")
            return {'CANCELLED'}

        _ensure_object_mode(context)

        segments = []
        for bone in arm.data.bones:
            part = _part_of_bone(bone.name)
            if part is None or not bone.use_deform or _is_ik_bone(bone.name):
                continue
            segments.append((part, bone.head_local.copy(),
                             bone.tail_local.copy()))
        if not segments:
            self.report({'ERROR'},
                        "No UE4-style deform bones found (thigh, upperarm, "
                        "spine, ...) - cannot split the mesh")
            return {'CANCELLED'}

        # Drop previous PART mask groups; per-bone overrides (vb:thigh_l)
        # are kept.
        for vg in list(mesh.vertex_groups):
            name = vg.name
            if name.startswith(VB_PREFIX) and name[len(VB_PREFIX):] in KNOWN_PARTS:
                mesh.vertex_groups.remove(vg)

        arm_inv = arm.matrix_world.inverted()
        mesh_mw = mesh.matrix_world
        vg_cache = {}
        counts = {}
        for vert in mesh.data.vertices:
            p = arm_inv @ (mesh_mw @ vert.co)
            best_part = None
            best_d = None
            for part, a, b in segments:
                d = _point_segment_dist(p, a, b)
                if best_d is None or d < best_d:
                    best_d, best_part = d, part
            if best_part is None:
                continue
            vg = vg_cache.get(best_part)
            if vg is None:
                name = VB_PREFIX + best_part
                vg = (mesh.vertex_groups.get(name)
                      or mesh.vertex_groups.new(name=name))
                vg_cache[best_part] = vg
            vg.add([vert.index], 1.0, 'REPLACE')
            counts[best_part] = counts.get(best_part, 0) + 1

        _update(context)
        summary = ", ".join("%s: %d" % kv for kv in sorted(counts.items()))
        print("[UE4 Binder] auto split parts (nearest bone): %s" % summary)
        self.report({'INFO'},
                    "Mask groups filled: %s. Fine-tune them in Edit Mode, "
                    "then run 5b" % summary)
        return {'FINISHED'}


class OBJECT_OT_ue4_bind_masked(Operator):
    bl_idname = "object.ue4_bind_masked"
    bl_label = "5b. Bind Masked Weights"
    bl_description = ("Parent mesh to armature and compute automatic "
                      "weights PER PART using vb: mask groups: bones of "
                      "each part only influence the vertices of their "
                      "group. Fixes arm bones stealing leg weights and "
                      "bandage/multi-layer problems")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return (props is not None and props.target_armature is not None
                and props.target_mesh is not None)

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        mesh = props.target_mesh

        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Set a valid armature first")
            return {'CANCELLED'}
        if not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid mesh first")
            return {'CANCELLED'}
        if mesh.parent is arm:
            self.report({'ERROR'}, "Mesh is already parented to this armature. Unbind first (Alt+P > Clear Parent and Keep Transform)")
            return {'CANCELLED'}

        _ensure_object_mode(context)

        # Apply mesh object rotation/scale (same as the plain bind).
        _select_only(context, [mesh], mesh)
        rot = mesh.matrix_world.to_quaternion()
        scl = mesh.matrix_world.to_scale()
        rot_dirty = (rot.w < 0.99999 or abs(rot.x) > 1e-5 or abs(rot.y) > 1e-5 or abs(rot.z) > 1e-5)
        scl_dirty = any(abs(s - 1.0) > 1e-5 for s in scl)
        if rot_dirty or scl_dirty:
            try:
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            except Exception as err:
                self.report({'WARNING'}, "Could not apply mesh transforms: %s" % err)

        deform = [b.name for b in arm.data.bones if b.use_deform]
        # IK bones (ik_foot_l, ik_hand_root, ...) never receive weights:
        # UE4 moves them every animation frame, so any weight on them
        # would corrupt the animation. They are excluded from all passes
        # and their stale groups are wiped below.
        weight_bones = [name for name in deform if not _is_ik_bone(name)]
        ik_skipped = len(deform) - len(weight_bones)
        deform_set = set(deform)
        vb_names = [vg.name for vg in mesh.vertex_groups
                    if vg.name.startswith(VB_PREFIX)]
        if not vb_names:
            self.report({'ERROR'},
                        "No 'vb:' vertex groups on the mesh. Run 5a (Auto "
                        "Split Parts) or create them by hand, or use the "
                        "plain '5. Bind'")
            return {'CANCELLED'}

        overrides = {}    # bone name -> vertex indices
        part_groups = {}  # part name -> vertex indices
        for vg_name in vb_names:
            key = vg_name[len(VB_PREFIX):]
            verts = _verts_of_group(mesh, vg_name)
            if not verts:
                print("[UE4 Binder] mask %s is empty - skipped" % vg_name)
                continue
            # Body parts take priority over bone names: 'head' is BOTH
            # a part and a bone name, 'vb:head' always means the part.
            if key in KNOWN_PARTS:
                part_groups[key] = verts
            elif _is_ik_bone(key):
                print("[UE4 Binder] WARNING: mask %s targets an IK bone -"
                      " IK bones stay weight-free, skipped" % vg_name)
            elif key in deform_set:
                overrides[key] = verts
            else:
                print("[UE4 Binder] WARNING: mask %s matches no bone and "
                      "no known part (%s) - skipped"
                      % (vg_name, ", ".join(KNOWN_PARTS)))
        if not part_groups and not overrides:
            self.report({'ERROR'},
                        "vb: groups are empty or unknown. Use part names "
                        "(%s) or bone names" % ", ".join(KNOWN_PARTS))
            return {'CANCELLED'}

        # --- build the passes: (bones, vertices, label) -------------
        # Per-bone override masks (vb:<bone>) are EXCLUSIVE: their
        # vertices are removed from all part passes, so those vertices
        # are weighted to the override bone ONLY.
        override_verts = set()
        for verts in overrides.values():
            override_verts.update(verts)
        if override_verts:
            print("[UE4 Binder] %d vertices are locked to per-bone masks "
                  "(vb:<bone>) - excluded from part passes"
                  % len(override_verts))

        passes = []
        used_bones = set()
        used_verts = set()
        for part in sorted(part_groups):
            bones = [b for b in weight_bones
                     if b not in overrides and _part_of_bone(b) == part]
            verts = [v for v in part_groups[part] if v not in override_verts]
            if not bones:
                print("[UE4 Binder] WARNING: vb:%s has no matching bones "
                      "in the armature - skipped" % part)
                continue
            if not verts:
                print("[UE4 Binder] vb:%s has no vertices left (all locked "
                      "to per-bone masks) - skipped" % part)
                continue
            passes.append((bones, verts, "vb:" + part))
            used_bones.update(bones)
            used_verts.update(verts)
        for bone in sorted(overrides):
            verts = overrides[bone]
            passes.append(([bone], verts, "vb:" + bone))
            used_bones.add(bone)
            used_verts.update(verts)

        loose_bones = [b for b in weight_bones if b not in used_bones]
        loose_verts = [i for i in range(len(mesh.data.vertices))
                       if i not in used_verts]
        if loose_bones and loose_verts:
            passes.append((loose_bones, loose_verts, "rest of mesh"))
            used_bones.update(loose_bones)
            used_verts.update(loose_verts)

        if loose_bones:
            print("[UE4 Binder] NOTE: %d bones have no mask and there are "
                  "no unassigned vertices - they get NO weights: %s"
                  % (len(loose_bones), ", ".join(loose_bones)))
        if loose_verts and not loose_bones:
            print("[UE4 Binder] NOTE: %d vertices are not in any vb: group "
                  "- they stay rigid (assign them or add vb:<part> groups)"
                  % len(loose_verts))

        # Wipe weights from previous bind attempts (masks are kept).
        # IK bone groups are wiped too - IK stays weight-free.
        _remove_ik_vertex_groups(mesh)
        for vg in list(mesh.vertex_groups):
            if vg.name in deform_set:
                mesh.vertex_groups.remove(vg)

        done = 0
        fallback = []
        failed_label = None
        error = None
        for bones, verts, label in passes:
            try:
                _auto_weights_pass(context, mesh, arm, bones, verts)
                done += 1
                print("[UE4 Binder] pass '%s': %d bones, %d vertices - ok"
                      % (label, len(bones), len(verts)))
                continue
            except Exception as heat_err:
                # Bone Heat can fail on tricky subsets (enclosed layers,
                # self-intersections). Fall back to distance weights for
                # THIS pass only instead of aborting the whole bind.
                print("[UE4 Binder] heat failed on pass '%s' (%s) - "
                      "distance-weight fallback" % (label, heat_err))
                try:
                    _distance_weights_pass(mesh, arm, bones, verts)
                    done += 1
                    fallback.append(label)
                    continue
                except Exception as dist_err:
                    failed_label, error = label, dist_err
                    break
        if error is not None:
            self.report({'ERROR'},
                        "Masked bind failed on pass '%s': %s. Fix the mesh "
                        "(holes/overlaps) and retry"
                        % (failed_label, error))
            return {'CANCELLED'}

        # Parent WITHOUT recomputing weights.
        bpy.ops.object.select_all(action='DESELECT')
        try:
            mesh.select_set(True)
        except Exception:
            pass
        try:
            arm.select_set(False)
        except Exception:
            pass
        context.view_layer.objects.active = arm
        try:
            bpy.ops.object.parent_set(type='ARMATURE')
        except Exception as err:
            self.report({'ERROR'}, "Parenting failed: %s" % err)
            return {'CANCELLED'}
        if mesh.parent is not arm:
            mesh.parent = arm
        if not any(m.type == 'ARMATURE' for m in mesh.modifiers):
            mod = mesh.modifiers.new(name="Armature", type='ARMATURE')
            mod.object = arm

        merged = 0
        if props.merge_fingers:
            merged = _merge_finger_weights(mesh)

        repaired, unresolved = _repair_zero_weights(mesh)
        _cleanup_deform_weights(mesh, deform)

        if props.reset_after_bind:
            _reset_pose(context, arm)
            _update(context)

        message = "Bound with masked weights (%d passes)" % done
        if ik_skipped:
            message += ", %d IK bones excluded" % ik_skipped
        if fallback:
            message += ", distance fallback on: %s" % ", ".join(fallback)
        if merged:
            message += ", %d finger groups merged" % merged
        if repaired:
            message += ", %d zero-weight vertices repaired" % repaired
        if unresolved:
            message += ", %d vertices still unweighted (check masks)" % unresolved
        if props.reset_after_bind:
            message += ". Pose reset"
        self.report({'WARNING' if unresolved else 'INFO'}, message)
        return {'FINISHED'}


class OBJECT_OT_ue4_repair_weights(Operator):
    bl_idname = "object.ue4_repair_weights"
    bl_label = "5c. Repair Zero Weights"
    bl_description = ("Copy weights from the nearest weighted vertex to "
                      "every vertex that has no weights (fixes layers "
                      "hidden under bandages/shells). Vertices inside "
                      "vb: groups only receive weights from the same "
                      "group")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_mesh is not None

    def execute(self, context):
        props = _props(context)
        mesh = props.target_mesh
        if not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid mesh first")
            return {'CANCELLED'}

        _ensure_object_mode(context)
        repaired, unresolved = _repair_zero_weights(mesh)
        _update(context)
        if not repaired and not unresolved:
            self.report({'INFO'}, "No zero-weight vertices found")
            return {'FINISHED'}
        message = "Repaired %d vertices" % repaired
        if unresolved:
            message += ", %d could not be fixed (no weighted source nearby)" % unresolved
        self.report({'WARNING' if unresolved else 'INFO'}, message)
        return {'FINISHED'}


class OBJECT_OT_ue4_clean_ik_weights(Operator):
    bl_idname = "object.ue4_clean_ik_weights"
    bl_label = "5d. Clean IK Bone Weights"
    bl_description = ("Delete every ik_* vertex group from the mesh. IK "
                      "bones (ik_foot_l, ik_hand_root, ...) drive UE4 "
                      "animation and must never carry skin weights - "
                      "weights on them corrupt animations. Bind steps 5 "
                      "and 5b clean them automatically")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_mesh is not None

    def execute(self, context):
        props = _props(context)
        mesh = props.target_mesh
        if not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid mesh first")
            return {'CANCELLED'}

        _ensure_object_mode(context)
        removed = _remove_ik_vertex_groups(mesh)
        _update(context)
        if removed:
            self.report({'INFO'},
                        "Removed %d IK vertex groups (ik_*)" % removed)
        else:
            self.report({'INFO'},
                        "No IK vertex groups found - mesh is clean")
        return {'FINISHED'}


class OBJECT_OT_ue4_reset_pose(Operator):
    bl_idname = "object.ue4_reset_pose"
    bl_label = "6. Reset Skeleton to Standard Pose"
    bl_description = "Zero all pose transforms. Skeleton returns to standard UE4 rest pose (mesh keeps its authored shape)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return props is not None and props.target_armature is not None

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Set a valid armature first")
            return {'CANCELLED'}
        _reset_pose(context, arm)
        _update(context)
        self.report({'INFO'}, "Skeleton reset to standard rest pose")
        return {'FINISHED'}


class OBJECT_OT_ue4_export_fbx(Operator):
    bl_idname = "object.ue4_export_fbx"
    bl_label = "7. Export FBX for UE4"
    bl_description = "Force standard rest pose and export armature + mesh as FBX ready for UE4 (use with 'Use Existing Skeleton')"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(
        name="File Path",
        description="Where to save the FBX",
        default="//character_ue4.fbx",
        subtype='FILE_PATH',
    )

    @classmethod
    def poll(cls, context):
        props = _props(context)
        return (props is not None and props.target_armature is not None
                and props.target_mesh is not None)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        props = _props(context)
        arm = props.target_armature
        mesh = props.target_mesh

        if not arm or arm.type != 'ARMATURE' or not mesh or mesh.type != 'MESH':
            self.report({'ERROR'}, "Set a valid armature and mesh first")
            return {'CANCELLED'}

        # The bind pose written to FBX must be the standard skeleton
        # pose, so force reset before export.
        _reset_pose(context, arm)
        _update(context)

        # Last line of defence: no IK weights may reach UE4 (they would
        # corrupt the animation). Removed groups are reported.
        ik_removed = _remove_ik_vertex_groups(mesh)
        if ik_removed:
            self.report({'WARNING'},
                        "Removed %d IK vertex groups (ik_*) before export"
                        % ik_removed)

        _select_only(context, [arm, mesh], arm)

        filepath = bpy.path.abspath(self.filepath)
        try:
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                use_selection=True,
                object_types={'ARMATURE', 'MESH'},
                use_mesh_modifiers=False,
                add_leaf_bones=False,
                bake_anim=False,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_NONE',
                mesh_smooth_type='FACE',
                path_mode='AUTO',
            )
        except Exception as err:
            self.report({'ERROR'}, "FBX export failed: %s" % err)
            return {'CANCELLED'}

        self.report({'INFO'}, "Exported: %s (rest pose = standard skeleton)" % filepath)
        return {'FINISHED'}


# --------------------------------------------------------------------
# UI
# --------------------------------------------------------------------

class VIEW3D_PT_ue4_binder_panel(Panel):
    bl_label = "UE4 Character Binder"
    bl_idname = "VIEW3D_PT_ue4_binder_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UE4 Binder"

    def draw(self, context):
        layout = self.layout
        props = _props(context)
        if props is None:
            layout.label(text="Addon not registered", icon='ERROR')
            return

        box = layout.box()
        box.label(text="UE4 Binder v%d.%d.%d" % bl_info["version"],
                  icon='INFO')
        box.label(text="Targets")
        box.prop(props, "target_armature", text="UE4 Armature")
        box.prop(props, "target_mesh", text="Character Mesh")

        box = layout.box()
        box.label(text="Options")
        box.prop(props, "pose_thumbs")
        box.prop(props, "merge_fingers")
        box.prop(props, "reset_after_bind")

        col = layout.column(align=True)
        col.operator("object.ue4_check_setup", text="1. Check Setup")
        col.operator("object.ue4_fit_height", text="1b. Fit Mesh Height (optional)")
        col.operator("object.ue4_create_markers", text="2. Create Joint Markers")
        col.operator("object.ue4_mirror_markers", text="3. Mirror Markers (L -> R)")
        col.operator("object.ue4_pose_to_markers", text="4. Pose Skeleton to Markers")
        col.operator("object.ue4_bind", text="5. Bind (Auto Weights)")
        col.operator("object.ue4_auto_split_parts", text="5a. Auto Split Parts (Nearest Bone)")
        col.operator("object.ue4_bind_masked", text="5b. Bind Masked Weights")
        col.operator("object.ue4_repair_weights", text="5c. Repair Zero Weights")
        col.operator("object.ue4_clean_ik_weights", text="5d. Clean IK Weights")
        col.operator("object.ue4_reset_pose", text="6. Reset Skeleton to Standard Pose")
        col.operator("object.ue4_export_fbx", text="7. Export FBX for UE4")

        box = layout.box()
        box.label(text="Weight masks (fix weight bleed)", icon='GROUP_VERTEX')
        box.label(text="vb:arms / vb:legs / vb:torso", icon='BLANK1')
        box.label(text="vb:head / vb:hands / vb:feet", icon='BLANK1')
        box.label(text="5a fills the groups automatically", icon='BLANK1')
        box.label(text="(nearest bone); edit by hand.", icon='BLANK1')
        box.label(text="5b weights each part separately:", icon='BLANK1')
        box.label(text="arm bones never touch leg verts,", icon='BLANK1')
        box.label(text="bandage + layer under it bind", icon='BLANK1')
        box.label(text="together. vb:<bone_name> limits", icon='BLANK1')
        box.label(text="a single bone. 5c heals verts", icon='BLANK1')
        box.label(text="that ended up unweighted.", icon='BLANK1')
        box.label(text="'Vertex not in group' lines", icon='BLANK1')
        box.label(text="in console during 5b are", icon='BLANK1')
        box.label(text="harmless solver noise.", icon='BLANK1')

        col = layout.column(align=True)
        col.label(text="Place LEFT + CENTER markers onto", icon='INFO')
        col.label(text="the character, then 3 (mirror)", icon='BLANK1')
        col.label(text="and 4 -> 5. Right side follows", icon='BLANK1')
        col.label(text="the mirror automatically.", icon='BLANK1')
        col.label(text="Step 4 snaps EVERY joint onto", icon='BLANK1')
        col.label(text="its marker (bones stretch in", icon='BLANK1')
        col.label(text="pose only - rest is untouched).", icon='BLANK1')
        col.label(text="Markers floating off the mesh", icon='BLANK1')
        col.label(text="are listed in System Console.", icon='BLANK1')
        col.label(text="Last bone of each chain", icon='BLANK1')
        col.label(text="keeps the mannequin angle.", icon='BLANK1')
        col.label(text="Never edit bones of the", icon='BLANK1')
        col.label(text="UE4 armature in Edit Mode.", icon='BLANK1')
        col.label(text="IK bones (ik_*) stay weight-", icon='BLANK1')
        col.label(text="free: 5 / 5b skip and clean", icon='BLANK1')
        col.label(text="them, 5d does it on demand.", icon='BLANK1')


# --------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------

classes = (
    UE4BinderProperties,
    OBJECT_OT_ue4_check_setup,
    OBJECT_OT_ue4_fit_height,
    OBJECT_OT_ue4_create_markers,
    OBJECT_OT_ue4_mirror_markers,
    OBJECT_OT_ue4_pose_to_markers,
    OBJECT_OT_ue4_bind,
    OBJECT_OT_ue4_auto_split_parts,
    OBJECT_OT_ue4_bind_masked,
    OBJECT_OT_ue4_repair_weights,
    OBJECT_OT_ue4_clean_ik_weights,
    OBJECT_OT_ue4_reset_pose,
    OBJECT_OT_ue4_export_fbx,
    VIEW3D_PT_ue4_binder_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ue4_binder_props = PointerProperty(type=UE4BinderProperties)


def unregister():
    if hasattr(bpy.types.Scene, "ue4_binder_props"):
        del bpy.types.Scene.ue4_binder_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
