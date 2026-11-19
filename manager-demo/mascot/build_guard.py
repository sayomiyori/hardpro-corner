"""Build the layered HARD-PRO headguard mascot in an isolated Blender process."""
import bpy, math, sys
from pathlib import Path
from mathutils import Vector
inputs, dest = map(Path,sys.argv[sys.argv.index("--")+1:])
bpy.ops.wm.read_factory_settings(use_empty=True)
scene=bpy.context.scene
scene.render.engine="CYCLES"
scene.cycles.samples=6
scene.render.film_transparent=True
scene.render.resolution_x=scene.render.resolution_y=320
scene.render.resolution_percentage=100
scene.render.fps=24
scene.frame_start=1;scene.frame_end=96
scene.view_settings.view_transform="Standard"

def empty(name,location=(0,0,0)):
    obj=bpy.data.objects.new(name,None);scene.collection.objects.link(obj);obj.location=location;return obj

root=empty("Guard • floating head")
pivot=empty("Eyes • blink pivot",(0,-.176,.012));pivot.parent=root
def layer(name,file,offset,parent):
    s=32; vertices=[];faces=[]
    for iy in range(s+1):
        for ix in range(s+1):
            x,y=ix*2/s-1,iy*2/s-1
            vertices.append((x,y+offset,.15*max(0,1-x*x-y*y)))
    for j in range(s):
        for i in range(s):
            k=j*(s+1)+i;faces.append((k,k+1,k+s+2,k+s+1))
    mesh=bpy.data.meshes.new(name);mesh.from_pydata(vertices,[],faces);mesh.update()
    uv=mesh.uv_layers.new(name="Artwork UV")
    for i,loop in enumerate(mesh.loops):
        n=loop.vertex_index;uv.data[i].uv=(n%(s+1)/s,n//(s+1)/s)
    obj=bpy.data.objects.new(name,mesh);scene.collection.objects.link(obj);obj.parent=parent
    mat=bpy.data.materials.new(name);mat.use_nodes=True;nt=mat.node_tree;nt.nodes.clear()
    tex=nt.nodes.new("ShaderNodeTexImage");tex.image=bpy.data.images.load(str(inputs/file));tex.image.pack()
    em=nt.nodes.new("ShaderNodeEmission");tr=nt.nodes.new("ShaderNodeBsdfTransparent");mix=nt.nodes.new("ShaderNodeMixShader");out=nt.nodes.new("ShaderNodeOutputMaterial")
    for a,b in [(tex.outputs['Color'],em.inputs['Color']),(tex.outputs['Alpha'],mix.inputs[0]),(tr.outputs[0],mix.inputs[1]),(em.outputs[0],mix.inputs[2]),(mix.outputs[0],out.inputs[0])]:nt.links.new(a,b)
    mesh.materials.append(mat);return obj
layer("Headguard • leather and visor","helmet.png",0,root)
layer("Eyes • mint glow","eyes.png",.176,pivot)
for frame,y,angle in [(1,0,-2),(25,.035,.8),(49,.05,2),(73,.025,-.5),(96,0,-2)]:
    root.location.y=y;root.rotation_euler.z=math.radians(angle);root.keyframe_insert(data_path="location",frame=frame);root.keyframe_insert(data_path="rotation_euler",frame=frame)
for frame,sy in [(1,1),(51,1),(53,.13),(55,1),(96,1)]:
    pivot.scale.y=sy;pivot.keyframe_insert(data_path="scale",frame=frame)
camera=bpy.data.cameras.new("Portrait");obj=bpy.data.objects.new("Portrait",camera);scene.collection.objects.link(obj);obj.location=(0,0,6);camera.type='ORTHO';camera.ortho_scale=2.18;scene.camera=obj
scene.frame_set(1);bpy.context.preferences.filepaths.save_version=0
bpy.ops.wm.save_as_mainfile(filepath=str(dest/'hardpro-guard.blend'))
scene.render.filepath=str(inputs/'blender-preview.png');bpy.ops.render.render(write_still=True)
