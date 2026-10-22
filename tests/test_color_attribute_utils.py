import unittest

import numpy as np

from utils.color_attribute_utils import (
    encode_color_for_unorm_target,
    get_color_storage_field,
    is_linear_color_storage,
    read_color_attribute_data,
    sample_color_attribute_to_loops,
    write_color_attribute_data,
)


class DummyAttributeData:
    def __init__(self, records, expected_field):
        self.records = np.asarray(records, dtype=np.float32).reshape(-1, 4)
        self.expected_field = expected_field
        self.last_field = None

    def __len__(self):
        return len(self.records)

    def foreach_get(self, field_name, output):
        self.last_field = field_name
        if field_name != self.expected_field:
            raise AssertionError(f"unexpected foreach_get field: {field_name}")
        output[:] = self.records.reshape(-1)

    def foreach_set(self, field_name, values):
        self.last_field = field_name
        if field_name != self.expected_field:
            raise AssertionError(f"unexpected foreach_set field: {field_name}")
        self.records = np.asarray(values, dtype=np.float32).reshape(-1, 4)


class DummyColorAttribute:
    def __init__(self, data_type, domain, records):
        self.data_type = data_type
        self.domain = domain
        self.data = DummyAttributeData(
            records=records,
            expected_field="color" if data_type == "FLOAT_COLOR" else "color_srgb",
        )


class DummyLegacyVertexColorLayer:
    def __init__(self, records):
        self.data = DummyAttributeData(records=records, expected_field="color")


class DummyLoops:
    def __init__(self, vertex_indices):
        self.vertex_indices = np.asarray(vertex_indices, dtype=np.int32)

    def __len__(self):
        return len(self.vertex_indices)

    def foreach_get(self, field_name, output):
        if field_name != "vertex_index":
            raise AssertionError(f"unexpected loop field: {field_name}")
        output[:] = self.vertex_indices


class DummyMesh:
    def __init__(self, vertex_indices):
        self.loops = DummyLoops(vertex_indices)


class ColorAttributeUtilsTests(unittest.TestCase):
    def test_byte_color_uses_srgb_storage_field(self):
        attr = DummyColorAttribute(
            data_type="BYTE_COLOR",
            domain="CORNER",
            records=[(0.1, 0.2, 0.3, 1.0)],
        )

        data = read_color_attribute_data(attr)

        self.assertEqual(get_color_storage_field(attr), "color_srgb")
        self.assertEqual(attr.data.last_field, "color_srgb")
        np.testing.assert_allclose(data, np.array([[0.1, 0.2, 0.3, 1.0]], dtype=np.float32))

    def test_float_color_uses_linear_storage_field(self):
        attr = DummyColorAttribute(
            data_type="FLOAT_COLOR",
            domain="CORNER",
            records=[(0.25, 0.5, 0.75, 1.0)],
        )

        data = read_color_attribute_data(attr)

        self.assertEqual(get_color_storage_field(attr), "color")
        self.assertEqual(attr.data.last_field, "color")
        np.testing.assert_allclose(data, np.array([[0.25, 0.5, 0.75, 1.0]], dtype=np.float32))

    def test_write_color_attribute_data_uses_matching_storage_field(self):
        attr = DummyColorAttribute(
            data_type="BYTE_COLOR",
            domain="CORNER",
            records=[(0.0, 0.0, 0.0, 0.0)],
        )

        write_color_attribute_data(attr, [(1.0, 0.5, 0.25, 1.0)])

        self.assertEqual(attr.data.last_field, "color_srgb")
        np.testing.assert_allclose(attr.data.records, np.array([[1.0, 0.5, 0.25, 1.0]], dtype=np.float32))

    def test_legacy_vertex_color_layer_uses_color_field(self):
        attr = DummyLegacyVertexColorLayer(records=[(0.2, 0.4, 0.6, 1.0)])

        data = read_color_attribute_data(attr)

        self.assertEqual(get_color_storage_field(attr), "color")
        self.assertEqual(attr.data.last_field, "color")
        np.testing.assert_allclose(data, np.array([[0.2, 0.4, 0.6, 1.0]], dtype=np.float32))

    def test_point_domain_is_expanded_to_loop_order(self):
        mesh = DummyMesh(vertex_indices=[2, 0, 1, 2])
        attr = DummyColorAttribute(
            data_type="BYTE_COLOR",
            domain="POINT",
            records=[
                (0.1, 0.1, 0.1, 1.0),
                (0.2, 0.2, 0.2, 1.0),
                (0.3, 0.3, 0.3, 1.0),
            ],
        )

        data = sample_color_attribute_to_loops(mesh, attr)

        expected = np.array(
            [
                (0.3, 0.3, 0.3, 1.0),
                (0.1, 0.1, 0.1, 1.0),
                (0.2, 0.2, 0.2, 1.0),
                (0.3, 0.3, 0.3, 1.0),
            ],
            dtype=np.float32,
        )
        np.testing.assert_allclose(data, expected)


class ColorStorageSpaceTests(unittest.TestCase):
    def test_float_color_is_the_only_linear_storage(self):
        self.assertTrue(
            is_linear_color_storage(
                DummyColorAttribute(data_type="FLOAT_COLOR", domain="CORNER", records=[(0.0, 0.0, 0.0, 1.0)])
            )
        )
        self.assertFalse(
            is_linear_color_storage(
                DummyColorAttribute(data_type="BYTE_COLOR", domain="CORNER", records=[(0.0, 0.0, 0.0, 1.0)])
            )
        )
        self.assertFalse(is_linear_color_storage(DummyLegacyVertexColorLayer(records=[(0.0, 0.0, 0.0, 1.0)])))
        self.assertFalse(is_linear_color_storage(None))

    def test_float_color_values_are_srgb_encoded_for_unorm_targets(self):
        linear_gray = 0.21586050  # sRGB 128 的线性值
        attr = DummyColorAttribute(
            data_type="FLOAT_COLOR",
            domain="CORNER",
            records=[(linear_gray, linear_gray, linear_gray, 0.25)],
        )

        encoded = encode_color_for_unorm_target(
            attr, np.array([[linear_gray, linear_gray, linear_gray, 0.25]], dtype=np.float32)
        )

        # RGB 回到 128/255，alpha 不参与编码
        np.testing.assert_allclose(
            encoded, np.array([[128 / 255, 128 / 255, 128 / 255, 0.25]], dtype=np.float32), atol=1e-6
        )

    def test_byte_and_legacy_values_pass_through_unchanged(self):
        values = np.array([[0.25, 0.5, 0.75, 0.5]], dtype=np.float32)
        byte_attr = DummyColorAttribute(
            data_type="BYTE_COLOR", domain="CORNER", records=[(0.25, 0.5, 0.75, 0.5)]
        )
        legacy_attr = DummyLegacyVertexColorLayer(records=[(0.25, 0.5, 0.75, 0.5)])

        np.testing.assert_array_equal(encode_color_for_unorm_target(byte_attr, values), values)
        np.testing.assert_array_equal(encode_color_for_unorm_target(legacy_attr, values), values)

    def test_unorm_encoding_clamps_out_of_range_values(self):
        attr = DummyColorAttribute(
            data_type="FLOAT_COLOR", domain="CORNER", records=[(2.0, -1.0, 0.5, 1.5)]
        )

        encoded = encode_color_for_unorm_target(attr, np.array([[2.0, -1.0, 0.5, 1.5]], dtype=np.float32))

        self.assertEqual(float(encoded[0, 0]), 1.0)
        self.assertEqual(float(encoded[0, 1]), 0.0)
        self.assertEqual(float(encoded[0, 3]), 1.0)
        np.testing.assert_allclose(float(encoded[0, 2]), 0.73535698, atol=1e-6)

    def test_empty_array_is_returned_untouched(self):
        attr = DummyColorAttribute(data_type="FLOAT_COLOR", domain="CORNER", records=[])

        encoded = encode_color_for_unorm_target(attr, np.zeros((0, 4), dtype=np.float32))

        self.assertEqual(encoded.shape, (0, 4))


if __name__ == "__main__":
    unittest.main()
