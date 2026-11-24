import os
import unittest
from unittest.mock import patch
from media_fp_guard import filter_synthetic_test_false_positives as filt


def word(txt, x, y, line):
    return dict(text=txt, left=x, top=y, width=max(len(txt)*12, 15), height=25, line_id=line)


def candidate(w, label='PERSON', value='?', source='zh'):
    return (w['left'], w['top'], w['left']+w['width'], w['top']+w['height'], label, value, 0.8, source)


class TestFalsePositiveGuard(unittest.TestCase):
    def setUp(self):
        self.hdr = [word('PII', 0, 0, 1), word('+', 44, 0, 1), word('FACE', 64, 0, 1)]
        self.card = [word('合成資料', 0, 40, 2), word('01', 75, 40, 2)]
        self.email = [word('Email：', 0, 80, 3), word('demo@example.com', 110, 80, 3)]
        self.name = [word('姓名：', 0, 120, 4), word('林家宇', 70, 120, 4)]
        self.phone = [word('電話：', 0, 160, 5), word('0912-345-678', 70, 160, 5)]
        self.words = self.hdr + self.card + self.email + self.name + self.phone
        self.candidates = [
            candidate(self.hdr[0], 'PERSON', 'PII'),
            candidate(self.card[-1], 'DATE_TIME', '01'),
            candidate(self.email[0], 'ORGANIZATION', 'Email'),
            candidate(self.name[0], 'PERSON', '姓名'),
            candidate(self.phone[0], 'ORGANIZATION', '電話'),
            candidate(self.email[1], 'EMAIL', 'demo@example.com', 'regex'),
            candidate(self.name[1], 'PERSON', '林家宇', 'field_regex_person'),
            candidate(self.phone[1], 'TW_PHONE', '0912-345-678', 'regex'),
            (210, 300, 260, 340, 'FACE', '[人臉]', 0.95, 'face_yunet'),
        ]

    def test_default_disabled(self):
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': ''}):
            k,s=filt(self.candidates,self.words)
        self.assertEqual(k,self.candidates)
        self.assertEqual(s,[])

    def test_only_known_scaffolding_removed(self):
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': 'synthetic_face_test'}):
            k,s=filt(self.candidates,self.words)
        self.assertEqual(len(s),5)
        self.assertEqual(len(k),4)
        self.assertEqual({b[4] for b in k},{'EMAIL','PERSON','TW_PHONE','FACE'})

    def test_dont_suppress_mixed_line_with_sensitive_value(self):
        mixed=[word('PII',0,0,1),word('+',40,0,1),word('FACE',70,0,1),word('demo@example.com',135,0,1)]
        fake=[candidate(mixed[0], 'PERSON', 'PII')]
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': 'synthetic_face_test'}):
            k,s=filt(fake,mixed)
        self.assertEqual(k,fake)
        self.assertEqual(s,[])

    def test_dont_suppress_protected_regex(self):
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': 'synthetic_face_test'}):
            k,s=filt([candidate(self.hdr[0], 'PERSON', 'PII', source='regex')],self.words)
        self.assertEqual(len(k),1)
        self.assertEqual(s,[])

    def test_dont_suppress_one_word_combined_email_label_value(self):
        mixed=[word('Email：demo@example.com',0,0,1)]
        fake=[candidate(mixed[0], 'ORGANIZATION', 'Email')]
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': 'synthetic_face_test'}):
            k,s=filt(fake,mixed)
        self.assertEqual(k,fake)
        self.assertEqual(s,[])

    def test_scale_coordinates(self):
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': 'synthetic_face_test'}):
            w = self.hdr[0]
            c=tuple(z/2 for z in candidate(w)[:4])+candidate(w)[4:]
            k,s=filt([c],self.hdr,scale=2)
        self.assertEqual(len(s),1)

    def test_refuses_broad_box_spilling_onto_other_line(self):
        with patch.dict(os.environ, {'PII_MEDIA_FP_PROFILE': 'synthetic_face_test'}):
            large=(0,0,500,160,'PERSON','PII',.7,'zh')
            k,s=filt([large],self.words)
        self.assertEqual(len(k),1)


if __name__ == '__main__':
    unittest.main()
