import { describe, expect, it } from 'vitest';
import { canSplitInside, markdownPrefix, repeatedHeader, sentenceStarts } from '../src';
import { block, lined } from './fixtures';

const sentences = (text: string) => {
  const starts = [0, ...sentenceStarts(text), text.length];
  return starts.slice(0, -1).map((s, i) => text.slice(s, starts[i + 1]).trim());
};

describe('sentenceStarts', () => {
  it('splits English sentences but not abbreviations, initials or decimals', () => {
    expect(sentences('See Fig. 2 for details. It costs 3.5 dollars, e.g. coffee. J. Smith agreed! Done?')).toEqual([
      'See Fig. 2 for details.',
      'It costs 3.5 dollars, e.g. coffee.',
      'J. Smith agreed!',
      'Done?',
    ]);
  });

  it('keeps closing quotes and brackets with the sentence', () => {
    expect(sentences('He said "stop." Then (finally.) we left.')).toEqual(['He said "stop."', 'Then (finally.) we left.']);
  });

  it('does not break when the next word is lowercase', () => {
    expect(sentences('Version 2. the next part continues.')).toEqual(['Version 2. the next part continues.']);
  });

  it('splits Korean sentences', () => {
    expect(sentences('문서는 자동으로 분할됩니다. 담당자가 검토합니다! 끝났나요? 네.')).toEqual([
      '문서는 자동으로 분할됩니다.',
      '담당자가 검토합니다!',
      '끝났나요?',
      '네.',
    ]);
  });

  it('splits Chinese and Japanese sentences without spaces', () => {
    expect(sentences('\u8fd9\u662f\u7b2c\u4e00\u53e5。\u8fd9\u662f\u7b2c\u4e8c\u53e5！\u7b2c\u4e09\u53e5？')).toEqual(['\u8fd9\u662f\u7b2c\u4e00\u53e5。', '\u8fd9\u662f\u7b2c\u4e8c\u53e5！', '\u7b2c\u4e09\u53e5？']);
    expect(sentences('これは\u6587です。「\u5f15\u7528です。」\u6b21の\u6587。')).toEqual(['これは\u6587です。', '「\u5f15\u7528です。」', '\u6b21の\u6587。']);
  });
});

describe('canSplitInside', () => {
  it('needs character positions, a splittable type and a markdown prefix', () => {
    expect(canSplitInside(lined('b1', 'Some text here.'))).toBe(true);
    expect(canSplitInside(block('b1', 'paragraph', 'Some text here.'))).toBe(false);
    expect(canSplitInside({ ...lined('b1', 'Heading'), type: 'section_header' })).toBe(false);
    expect(canSplitInside({ ...lined('b1', 'x = 1'), type: 'list_item', markdown: '1. x = 1' })).toBe(true);
    expect(canSplitInside({ ...lined('b1', 'a b'), markdown: '```\na b\n```' })).toBe(false);
    expect(markdownPrefix({ ...lined('b1', 'x'), markdown: '- x' })).toBe('- ');
  });
});

describe('repeatedHeader', () => {
  it('repeats column names but not a first row of data', () => {
    const named = '| 구분 | 2019 | 2020 |\n| --- | --- | --- |\n| 매출 | 100 | 200 |\n| 이익 | 10 | 20 |';
    expect(repeatedHeader(named)).toBe('| 구분 | 2019 | 2020 |\n| --- | --- | --- |');
    const cited = '| 5,208,634 A | 5/1993 Ikemoto et al. |\n| --- | --- |\n| 5,223,893 A | 6/1993 Ikemoto et al. |\n| 6,823,155 B2 | 11/2004 Tsuda et al. |';
    expect(repeatedHeader(cited)).toBe('');
    const contents = '| 1. | Introduction | 1 |\n| --- | --- | --- |\n| 2. | Preliminaries | 3 |\n| 3. | Results | 9 |';
    expect(repeatedHeader(contents)).toBe('');
    const empty = '|  |  |  | 1 |\n| --- | --- | --- | --- |\n| name11 | 610 | 85 | 1 |\n| name12 | 563 | 105 | 1 |';
    expect(repeatedHeader(empty)).toBe('');
  });
});
