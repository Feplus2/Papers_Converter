"""元数据加固单测：下载水印剔除 + 出版年优先级裁决（wang2024routes 实证形态）。"""

import unittest

import metadata as md

WANG_FIRST_PAGE = """Routes to high-performance layered oxide cathodes for sodium-ion batteries
Jingqiang Wang, Yan-Fang Zhu, Yu Su, Jun-Xu Guo, Shuangqiang Chen
Received 3rd December 2023, Accepted 14th February 2024
DOI: 10.1039/d3cs00929g
Chem. Soc. Rev., 2024, 53, 4230–4327
Published on 13 March 2024. Downloaded by Shanghai Jiaotong University on 12/17/2025 10:11:26 AM."""


class TestWatermarkFilter(unittest.TestCase):
    def test_rsc_published_downloaded_line(self):
        out = md._strip_download_watermark(
            "Published on 13 March 2024. Downloaded by Shanghai Jiaotong "
            "University on 12/17/2025 10:11:26 AM.")
        self.assertIn("Published on 13 March 2024.", out)
        self.assertNotIn("Downloaded", out)
        self.assertNotIn("2025", out)

    def test_full_watermark_lines_dropped(self):
        text = ("Downloaded from 137.189.10.20 on 12/17/2025 10:11:26 AM\n"
                "via University Library proxy 10.1.2.3\n"
                "Body text stays.")
        out = md._strip_download_watermark(text)
        self.assertNotIn("Downloaded", out)
        self.assertNotIn("137.189", out)
        self.assertIn("Body text stays.", out)

    def test_body_lines_untouched(self):
        # 正文里的下载字样（非水印形态）不误伤
        text = ("Users can download the dataset from our website.\n"
                "We acknowledge the data download portal maintainers.")
        out = md._strip_download_watermark(text)
        self.assertIn("download the dataset", out)
        self.assertIn("download portal", out)


class TestYearArbitration(unittest.TestCase):
    MAXY = 2027

    def test_wang_three_years(self):
        y, src = md._pick_pub_year([md._strip_download_watermark(WANG_FIRST_PAGE)],
                                   self.MAXY)
        self.assertEqual(y, "2024")
        self.assertNotEqual(src, "received")  # 不得取收稿/下载年

    def test_received_only_fallback(self):
        y, src = md._pick_pub_year(["Received 5 May 2023\nSome body text"], self.MAXY)
        self.assertEqual((y, src), ("2023", "received"))

    def test_accepted_beats_received(self):
        y, src = md._pick_pub_year(
            ["Received 5 May 2022, Accepted 3 March 2023"], self.MAXY)
        self.assertEqual((y, src), ("2023", "accepted"))

    def test_over_window_rejected(self):
        # 超窗年份（>今年+1）判伪并降级到 received
        y, src = md._pick_pub_year(
            ["Published on 3 March 2099", "Received 5 May 2023"], self.MAXY)
        self.assertEqual((y, src), ("2023", "received"))

    def test_watermark_year_never_picked(self):
        y, _ = md._pick_pub_year(
            [md._strip_download_watermark(
                "Downloaded by X on 12/17/2025\nReceived 5 May 2023")], self.MAXY)
        self.assertEqual(y, "2023")

    def test_no_candidate(self):
        y, _ = md._pick_pub_year(["no dates here"], self.MAXY)
        self.assertIsNone(y)

    def test_volume_year_page_citation_line(self):
        # ernst 形态："Mater. Chem. Phys. 101 (2007) 372" 引用行优先于
        # received/accepted 时间线
        y, src = md._pick_pub_year(
            ["Materials Chemistry and Physics 101 (2007) 372–378\n"
             "Received 26 October 2005; received in revised form 26 February 2006; "
             "accepted 10 April 2006"], self.MAXY)
        self.assertEqual(y, "2007")
        self.assertEqual(src, "引用行")


if __name__ == "__main__":
    unittest.main()
