"""Executed by Blender, not by the normal Python runtime."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def arg(name: str) -> str:
    args = sys.argv[sys.argv.index("--") + 1:]
    return args[args.index(name) + 1]


def material(name, color, metallic=0.0, roughness=0.55):
    value = bpy.data.materials.new(name)
    value.diffuse_color = (*color, 1)
    value.use_nodes = True
    shader = value.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Metallic"].default_value = metallic
    shader.inputs["Roughness"].default_value = roughness
    return value


def cube(name, location, scale, mat, bevel=.08):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name, obj.scale = name, scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new("Soft edges", "BEVEL")
        mod.width, mod.segments = bevel, 3
    return obj


def import_character(path: str, name: str, x: float):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path, use_anim=True, automatic_bone_orientation=True)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    parent = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(parent)
    for obj in imported:
        if obj.parent is None:
            obj.parent = parent
        if obj.type == "ARMATURE":
            current = obj.animation_data.action if obj.animation_data else None
            preferred = current or next(
                (a for a in bpy.data.actions if any(word in a.name.lower() for word in ("idle", "talk", "interact", "work"))),
                None,
            )
            if preferred:
                obj.animation_data_create()
                obj.animation_data.action = preferred
    parent.location = (x, 0, 0)
    # Asset packs differ in export scale. Normalize by the visible bounding box.
    bpy.context.view_layer.update()
    points = [parent.matrix_world @ Vector(corner) for obj in imported if obj.type == "MESH" for corner in obj.bound_box]
    if points:
        height = max(p.z for p in points) - min(p.z for p in points)
        if height > 0:
            parent.scale = (2.0 / height,) * 3
    return parent, imported


def setup_world(scene_data):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.curves, bpy.data.cameras, bpy.data.lights):
        pass
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (.005, .009, .02, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = .28
    dark = material("Midnight", (.018, .03, .065), roughness=.72)
    steel = material("Steel", (.08, .13, .19), metallic=.25, roughness=.34)
    cyan = material("Screen cyan", (.015, .7, .78), metallic=.1, roughness=.2)
    floor = cube("Floor", (0, 0, -.12), (8, 6, .12), dark)
    location = str(scene_data.get("location", "")).lower()
    if any(x in location for x in ("zentrale", "büro", "leitstelle", "office")):
        cube("Desk", (0, .7, .72), (3.1, .65, .08), steel)
        for x in (-2.1, 0, 2.1):
            cube("Monitor", (x, .95, 1.55), (.72, .06, .45), steel)
            cube("Display", (x, .88, 1.55), (.63, .025, .36), cyan, .03)
    elif any(x in location for x in ("gang", "flur", "tür", "keller")):
        cube("Back wall", (0, 2.2, 2.1), (6, .12, 2.2), dark)
        cube("Door", (0, 2.0, 1.45), (1.05, .10, 1.45), steel)
    else:
        for x in (-4, -2, 0, 2, 4):
            cube("Column", (x, 2.7, 1.5), (.22, .22, 1.5), steel)
    return floor


def add_camera_and_lights(scene_index):
    bpy.ops.object.light_add(type="AREA", location=(-3.5, -2.5, 5))
    bpy.context.object.data.energy, bpy.context.object.data.shape, bpy.context.object.data.size = 1100, "DISK", 5
    bpy.context.object.data.color = (.28, .55, 1)
    bpy.ops.object.light_add(type="AREA", location=(4, 1, 3.5))
    bpy.context.object.data.energy, bpy.context.object.data.size = 900, 4
    bpy.context.object.data.color = (1, .2, .28)
    bpy.ops.object.light_add(type="AREA", location=(0, -3, 2.4))
    bpy.context.object.data.energy, bpy.context.object.data.size = 700, 3
    bpy.ops.object.camera_add(location=((.35 if scene_index % 2 else -.35), -7.8, 2.15))
    camera = bpy.context.object
    camera.data.lens = 53 if scene_index % 3 else 42
    bpy.context.scene.camera = camera
    target = Vector((0, .25, 1.3))
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.keyframe_insert("location", frame=1)
    camera.location.y += .55
    camera.keyframe_insert("location", frame=int(arg("--frames")))


def main():
    config = json.loads(Path(arg("--config")).read_text(encoding="utf-8"))
    scene_index = int(arg("--scene-index"))
    output = arg("--output")
    frames = int(arg("--frames"))
    assets = json.loads(Path(arg("--assets")).read_text(encoding="utf-8"))
    setup_world(config)
    imported = []
    positions = (-1.45, 1.45)
    actor_count = 0
    for path in assets:
        if actor_count >= 2:
            break
        try:
            parent, objects = import_character(
                path, f"Actor {actor_count + 1}", positions[actor_count]
            )
            if not any(obj.type == "ARMATURE" for obj in objects):
                for obj in objects + [parent]:
                    bpy.data.objects.remove(obj, do_unlink=True)
                continue
            imported.extend(objects)
            # Conversation blocking: actors face one another, then shift with emotion.
            parent.rotation_euler.z = (-.18 if actor_count == 0 else .18)
            parent.keyframe_insert("rotation_euler", frame=1)
            parent.rotation_euler.z += (.08 if actor_count == 0 else -.08)
            parent.keyframe_insert("rotation_euler", frame=frames // 2)
            parent.rotation_euler.z -= (.05 if actor_count == 0 else -.05)
            parent.keyframe_insert("rotation_euler", frame=frames)
            actor_count += 1
        except Exception as exc:
            print(f"Skipping incompatible FBX {path}: {exc}")
    if len([x for x in imported if x.type == "MESH"]) < 2:
        raise RuntimeError("The CC0 asset pack did not expose two importable animated characters")
    add_camera_and_lights(scene_index)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, frames
    scene.render.engine = "BLENDER_EEVEE_NEXT" if bpy.app.version >= (4, 2, 0) else "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1280, 720, 100
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.ffmpeg.ffmpeg_preset = "GOOD"
    scene.render.filepath = output
    scene.render.fps = 24
    scene.render.film_transparent = False
    if bpy.app.version >= (4, 0, 0):
        scene.view_settings.look = "AgX - Medium High Contrast"
    bpy.ops.render.render(animation=True)


main()
