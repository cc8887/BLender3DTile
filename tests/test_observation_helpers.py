"""Pure helper tests run without importing Blender."""
import unittest

from b3dtile.observation import _unique_colors, bounds, id_color


class ObservationHelpersTests(unittest.TestCase):
    def test_bounds_rejects_nonfinite_and_empty_geometry(self):
        for points in ([], [(0, 0, float("nan"))], [(0, 0, float("inf"))], [(0, 1)]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                bounds(points)

    def test_bounds_includes_translated_geometry(self):
        result = bounds([(5, -2, 0), (7, 2, 4), (6, 0, 1)])
        self.assertEqual(result, {"min": [5, -2, 0], "max": [7, 2, 4],
                                 "center": [6, 0, 2], "dimensions": [2, 4, 4]})

    def test_colors_are_stable_when_objects_are_reordered(self):
        self.assertEqual(_unique_colors(["roof", "wall"]), _unique_colors(["wall", "roof"]))
        self.assertNotEqual(id_color("roof"), id_color("wall"))
        self.assertTrue(all(0 <= channel <= 255 for channel in id_color("roof")))

    def test_actual_pixels_uv_and_geometry_have_independent_fingerprints(self):
        try:
            import bpy
        except ImportError:
            self.skipTest("Blender backend is not installed")
        from b3dtile.model import KitInstance
        from b3dtile.observation import _fingerprints

        mesh = bpy.data.meshes.new("fingerprint_test_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        uv = mesh.uv_layers.new()
        obj = bpy.data.objects.new("fingerprint_test_object", mesh)
        image = bpy.data.images.new("fingerprint_test_image", width=2, height=2)
        material = bpy.data.materials.new("fingerprint_test_material")
        material.use_nodes = True
        texture = material.node_tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        mesh.materials.append(material)
        instance = KitInstance("sample", "sample", obj, obj, {"body": [0]}, {})
        try:
            original = _fingerprints(instance)
            image.pixels[0] = 0.5
            painted = _fingerprints(instance)
            self.assertEqual(original["geometry_sha256"], painted["geometry_sha256"])
            self.assertNotEqual(original["appearance_sha256"], painted["appearance_sha256"])
            uv.data[0].uv.x = 0.25
            remapped = _fingerprints(instance)
            self.assertEqual(painted["geometry_sha256"], remapped["geometry_sha256"])
            self.assertNotEqual(painted["appearance_sha256"], remapped["appearance_sha256"])
            mesh.vertices[0].co.z = 0.5
            moved = _fingerprints(instance)
            self.assertNotEqual(remapped["geometry_sha256"], moved["geometry_sha256"])
            self.assertEqual(remapped["appearance_sha256"], moved["appearance_sha256"])
        finally:
            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.meshes.remove(mesh)
            bpy.data.materials.remove(material)
            bpy.data.images.remove(image)


if __name__ == "__main__":
    unittest.main()
