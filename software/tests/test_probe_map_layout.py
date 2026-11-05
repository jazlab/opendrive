"""Contact pads keep uniform dimensions, including edge rows and columns."""
import tkinter as tk
import gc
import unittest
from unittest.mock import patch

from opendrive.probe_map_layout import probe_map_layout
from opendrive.probe_summary_viewer import ProbeSummaryViewer
from opendrive.probe_view_settings import DEFAULT_PROBE_VIEW_SETTINGS


def staggered_geometry():
    xs, zs = [], []
    for row in range(192):
        xs.extend([.016 * (row % 2), .032 + .016 * (row % 2)])
        zs.extend([row * .02, row * .02])
    return xs, zs


class ProbeMapLayoutTests(unittest.TestCase):
    def test_probe_viewer_defaults_and_reopening_restore_all_six_settings(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        saved = []
        try:
            with patch.object(ProbeSummaryViewer, '_restart'):
                first = ProbeSummaryViewer(root, on_settings_change=saved.append)
                self.assertEqual(first.get_settings(), DEFAULT_PROBE_VIEW_SETTINGS)
                first._min_var.set('.01')
                first._max_var.set('.3')
                first._update_ms_var.set('1000')
                first._horizontal_pixels_var.set('25')
                first._vertical_pixels_var.set('8')
                first._spacing_var.set('2')
                first.destroy()  # Include valid edits even without clicking Apply.
                second = ProbeSummaryViewer(root, initial_settings=saved[-1])
                self.assertEqual(second.get_settings(), dict(min_mv=.01, max_mv=.3, update_ms=1000,
                    horizontal_pixels=25, vertical_pixels=8, spacing=2))
                self.assertEqual(second._layout_settings, (25, 8, 2))
                second.destroy()
        finally:
            for job in root.tk.call('after', 'info'):
                root.after_cancel(job)
            root.destroy()
        first = second = root = None
        gc.collect()

    def test_staggered_outer_channels_are_full_size_at_multiple_window_sizes(self):
        xs, zs = staggered_geometry()
        for width, height in ((180, 360), (520, 520), (900, 680), (1400, 950)):
            with self.subTest(size=(width, height)):
                bodies, pads = probe_map_layout(384, xs, zs, [0] * 384, 0, width, height)
                self.assertEqual(len(pads), 384)
                sizes = {(x1 - x0, y1 - y0) for _, (x0, y0, x1, y1) in pads}
                self.assertEqual(len(sizes), 1)
                self.assertEqual(sizes, {(20, 6)})
                _, left, right, top, bottom = bodies[0]
                for channel, (x0, y0, x1, y1) in pads:
                    self.assertGreater(x0, left)
                    self.assertLess(x1, right)
                    self.assertGreater(y0, top)
                    self.assertLess(y1, bottom)
                coordinates = dict(pads)
                self.assertLess(coordinates[0][0], coordinates[2][0])  # staggered next row
                self.assertGreater(coordinates[0][1], coordinates[382][1])  # tip at bottom

    def test_multiple_shanks_have_separate_outlines_and_equal_contact_sizes(self):
        xs, zs = [.0, .032, .0, .032] * 4, [0, 0, .02, .02] * 4
        shanks = [index // 4 for index in range(16)]
        bodies, pads = probe_map_layout(16, xs, zs, shanks, 0, 800, 600)
        self.assertEqual(len(bodies), 4)
        self.assertEqual(len(pads), 16)
        sizes = {(x1 - x0, y1 - y0) for _, (x0, y0, x1, y1) in pads}
        self.assertEqual(len(sizes), 1)
        for channel, (x0, _, x1, _) in pads:
            self.assertGreater(x0, bodies[shanks[channel]][1])
            self.assertLess(x1, bodies[shanks[channel]][2])

    def test_missing_geometry_has_a_uniform_single_column_fallback(self):
        bodies, pads = probe_map_layout(12, [], [], [], 0, 400, 600)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(len(pads), 12)
        self.assertEqual(len({(x1 - x0, y1 - y0) for _, (x0, y0, x1, y1) in pads}), 1)
        self.assertEqual(len({x0 for _, (x0, _, _, _) in pads}), 1)

    def test_independent_pixel_dimensions_and_spacing_are_exact(self):
        xs, zs = staggered_geometry()
        bodies, pads = probe_map_layout(384, xs, zs, [0] * 384, 0, 100, 600,
                                        horizontal_pixels=30, vertical_pixels=8, spacing=5)
        self.assertEqual({(x1 - x0, y1 - y0) for _, (x0, y0, x1, y1) in pads}, {(30, 8)})
        self.assertGreater(bodies[0][4], 600)
        self.assertEqual(len(pads), 384)
        # Adjacent contacts with one geometry pitch between their centers.
        _, simple = probe_map_layout(4, [0, .016, 0, .016], [0, 0, .02, .02], [0]*4,
                                    0, 300, 100, horizontal_pixels=30, vertical_pixels=8, spacing=5)
        rects = dict(simple)
        self.assertEqual(rects[1][0] - rects[0][2], 5)
        self.assertEqual(rects[0][1] - rects[2][3], 5)

    def test_live_canvas_preserves_equal_widths_and_shank_identity(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)
        try:
            with patch.object(ProbeSummaryViewer, '_restart'):
                viewer = ProbeSummaryViewer(root)
                viewer.geometry('900x820+30+30')
                root.update()
                xs, zs = staggered_geometry()
                viewer._values_by_probe = {0: [.05] * 384}
                viewer._x_by_probe = {0: xs}
                viewer._depths_by_probe = {0: zs}
                viewer._shanks_by_probe = {0: [0] * 192 + [1] * 192}
                viewer._draw_probe()
                self.assertEqual(len(viewer._canvas.find_withtag('shank')), 2)
                contacts = viewer._canvas.find_withtag('contact')
                self.assertEqual(len(contacts), 384)
                labels = viewer._canvas.find_withtag('probe_label')
                self.assertEqual([viewer._canvas.itemcget(item, 'text') for item in labels], ['imec0'])
                self.assertGreater(viewer._canvas.coords(labels[0])[1],
                                   max(viewer._canvas.coords(item)[3] for item in contacts))
                sizes = set()
                for item in contacts:
                    x0, y0, x1, y1 = viewer._canvas.coords(item)
                    sizes.add((x1 - x0, y1 - y0))
                self.assertEqual(len(sizes), 1)
                self.assertEqual(sizes, {(20., 6.)})
                viewer._canvas.yview_moveto(1.)
                before = viewer._canvas.yview()
                self.assertGreater(before[0], 0.)
                viewer._values_by_probe[0] = [.1] * 384
                viewer._draw_probe()
                self.assertEqual(viewer._canvas.yview(), before)
                with patch.object(viewer, '_restart') as restart:
                    viewer._on_view_settings()
                    popup = viewer._view_settings_window
                    self.assertTrue(popup.winfo_exists())
                    viewer._on_view_settings()
                    self.assertIs(viewer._view_settings_window, popup)
                    viewer._horizontal_pixels_var.set('35')
                    viewer._vertical_pixels_var.set('9')
                    viewer._spacing_var.set('4')
                    viewer._apply_layout()
                    self.assertEqual(viewer._layout_settings, (35, 9, 4))
                    sizes = set()
                    for item in viewer._canvas.find_withtag('contact'):
                        x0, y0, x1, y1 = viewer._canvas.coords(item)
                        sizes.add((x1-x0, y1-y0))
                    self.assertEqual(sizes, {(35., 9.)})
                    viewer._horizontal_pixels_var.set('256')
                    viewer._apply_layout()
                    self.assertLess(viewer._canvas.xview()[1] - viewer._canvas.xview()[0], 1.)
                    self.assertTrue(all(viewer._canvas.coords(item)[2] - viewer._canvas.coords(item)[0] == 256
                        for item in viewer._canvas.find_withtag('contact')))
                    viewer._vertical_pixels_var.set('')
                    viewer._draw_probe()  # A data frame during partial typing is safe.
                    viewer._apply_layout()
                    self.assertEqual(viewer._vertical_pixels_var.get(), '9')
                    restart.assert_not_called()
                viewer.destroy()
        finally:
            for job in root.tk.call('after', 'info'):
                root.after_cancel(job)
            root.destroy()
        viewer = popup = root = None
        gc.collect()
        self.assertEqual(errors, [])
