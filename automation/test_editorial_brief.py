"""Event-level reading tests; all source links remain available."""

import copy
import unittest

import editorial_brief as editorial
import render_daily as renderer


def item(identifier, title, summary, source_id="maoyan-film-news", *,
         published="2026-09-30T09:00:00Z", market="m-cn", confidence="single_source",
         source_class="trade_media", distribution="cinema"):
    url = f"https://example.com/{identifier}"
    return {"id": identifier, "title": title, "summary": summary, "url": url,
            "published_at": published, "published": published, "market": market,
            "confidence": confidence, "distribution": distribution,
            "entry_type": "电影动态", "sources": [{"id": source_id, "publisher_id": source_id,
            "name": source_id, "class": source_class, "url": url}]}


class EditorialBriefTests(unittest.TestCase):
    def test_same_release_event_merges_different_headlines_and_keeps_links(self):
        first = item("maoyan", "电影《疾速追杀》定档10月30日内地公映",
                     "电影《疾速追杀》10月30日内地上映。")
        second = item("mtime", "《疾速追杀》确认10月30日登陆内地院线",
                      "《疾速追杀》第一部首次在内地公映，日期为10月30日。",
                      source_id="mtime-news")
        payload = {"items": [first, second], "policy": {"editorial_budget": {"m-cn": 10}},
                   "stats": {"market_coverage": {"m-cn": {"items": 2}}}}
        result = editorial.make_brief(payload)
        self.assertEqual(result["stats"]["brief_events"], 1)
        self.assertEqual(result["stats"]["source_items"], 2)
        self.assertEqual(result["items"][0]["confidence"], "single_source")
        self.assertEqual({s["url"] for s in result["items"][0]["sources"]},
                         {first["url"], second["url"]})
        self.assertEqual(result["stats"]["merged_source_items"], 1)

    def test_different_box_office_milestones_remain_separate(self):
        first = item("one", "《甲片》票房破1亿", "据平台，《甲片》票房突破1亿元。")
        second = item("two", "《甲片》票房破2亿", "据平台，《甲片》票房突破2亿元。")
        self.assertNotEqual(editorial.event_key(first), editorial.event_key(second))

    def test_award_list_and_individual_nominations_form_one_event(self):
        general = item("list", "第35届飞天奖入围名单公布", "第35届电视剧飞天奖共48部作品入围。")
        specific = item("show", "《北上》提名飞天奖", "第35届电视剧飞天奖名单公布，《北上》提名。",
                        source_id="taopiaopiao-official-posts", confidence="social_lead",
                        source_class="social", distribution="series")
        self.assertEqual(editorial.event_key(general), editorial.event_key(specific))

    def test_filtered_and_lower_priority_items_keep_original_urls(self):
        release = item("release", "《甲片》定档10月1日", "《甲片》定档10月1日上映。")
        promo = item("promo", "《乙片》上映三周年", "回顾《乙片》的故事。",
                     source_id="taopiaopiao-official-posts", source_class="social",
                     confidence="social_lead")
        other = item("other", "《丙片》导演访谈", "导演谈影片创作过程与镜头设计，并回顾剧本开发中的主要选择。")
        payload = {"items": [release, promo, other],
                   "policy": {"editorial_budget": {"m-cn": 1}}, "stats": {}}
        result = editorial.make_brief(copy.deepcopy(payload))
        self.assertEqual(result["stats"]["brief_events"], 1)
        self.assertEqual(result["stats"]["filtered_low_signal"], 1)
        self.assertEqual(result["stats"]["more_events"], 1)
        self.assertEqual({i["url"] for i in result["source_items"]},
                         {release["url"], promo["url"], other["url"]})
        pool = renderer.render_source_pool(result)
        self.assertIn(promo["url"], pool)
        self.assertIn(other["url"], pool)

    def test_editorial_card_does_not_duplicate_title_as_summary(self):
        source = item("story", "《甲片》定档10月1日", "《甲片》定档10月1日")
        card = editorial.assemble([source])
        rendered = renderer.render_item(card, {"single_source": "媒体报道"})
        self.assertNotIn('class="r-lead"', rendered)
        self.assertIn("1 条原文", rendered)

    def test_award_and_trailer_reposts_merge_across_observation_market(self):
        mainland = item("mainland", "蔡成杰平遥获奖！《四分之四》曝先导片",
                        "导演蔡成杰斩获“费穆荣誉·特别表扬”。影片发布先导片。")
        observation = item("observation", "蔡成杰平遥喜夺费穆荣誉·特别表扬！《四分之四》曝先导片",
                           mainland["summary"], market="m-obs", source_id="mtime-news")
        result = editorial.make_brief({"items": [mainland, observation], "stats": {}})
        self.assertEqual(result["stats"]["brief_events"], 1)
        self.assertEqual(result["items"][0]["market"], "m-cn")
        self.assertIn("费穆荣誉·特别表扬",
                      result["items"][0]["title"] + result["items"][0]["summary"])

    def test_long_publicity_copy_becomes_a_factual_material_update(self):
        source = item("trailer", "《重生2》释“全员反差”特别视频 梁洛施文俊辉深陷命运抉择",
                      "今日，由十多位演员主演的电影《重生2》发布“全员反差”特别视频，" +
                      "展露各个角色在不同情况下的人生切面，尽显全员狠人的硬核对峙。" * 5)
        self.assertEqual(editorial.core_sentence(source), "《重生2》发布“全员反差”特别视频")

    def test_award_result_extracts_winners_instead_of_ceremony_description(self):
        source = item("awards", "第33届中国电视金鹰奖颁奖典礼落幕",
                      "9月29日晚颁奖典礼闭幕。于和伟凭借《沉默的荣耀》获最佳男主角，"
                      "宋佳凭借《山花烂漫时》获最佳女主角。")
        self.assertEqual(editorial.core_sentence(source),
                         "于和伟凭《沉默的荣耀》获最佳男主角，宋佳凭《山花烂漫时》获最佳女主角")


if __name__ == "__main__":
    unittest.main()
