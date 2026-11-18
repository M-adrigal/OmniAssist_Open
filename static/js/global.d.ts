/* global.d.ts — DOM 类型补充声明
 *
 * 为什么需要：
 *   static/js 是原生 JS 平铺加载（无模块、无构建）。代码大量用
 *   `querySelector(...)` 取元素后直接读 value / dataset / checked，
 *   或从 `e.target` 上读 closest / naturalWidth。浏览器里没问题，但 DOM
 *   标准库把 querySelector 声明成返回 Element、把 e.target 声明成
 *   EventTarget，这些成员在两者上都不存在，于是 TypeScript 逐处报错。
 *
 *   这不是缺陷，是「选择器是运行时字符串，静态类型无从推断」。
 *
 * 做法：只补充「标准库里确实没有」的成员，不动已有声明（动了会和标准库
 * 冲突，报 TS8030）。这样存量代码通过，而真正的问题——属性名拼错、调用
 * 不存在的方法、参数个数不对——依然会被抓出来。
 */

interface Element {
  /** 表单元素的值。非表单元素上为 undefined。 */
  value?: string;
  /** 复选框 / 单选框的选中态。 */
  checked?: boolean;
  /** 控件禁用态。 */
  disabled?: boolean;
  /** 输入框占位文案。 */
  placeholder?: string;
  /** 只读态。 */
  readOnly?: boolean;
  /** input 的 type 属性（text / password / file …）。 */
  type?: string;
  /** data-* 属性集合（标准库里只在 HTMLElement 上有）。 */
  dataset?: DOMStringMap;
  /** 文本内容（标准库里只在 HTMLElement 上有）。 */
  innerText?: string;
  /** 提示文案（标准库里只在 HTMLElement 上有）。 */
  title?: string;
  /** 焦点相关（标准库里只在 HTMLElement 上有）。 */
  focus?: () => void;
  blur?: () => void;
  select?: () => void;
  setSelectionRange?: (start: number, end: number, dir?: string) => void;
  /** 内嵌文档（iframe / frame）。 */
  contentDocument?: Document | null;
  contentWindow?: Window | null;
}

interface EventTarget {
  /** 事件目标上的常用成员，供未收窄的 e.target 使用。 */
  closest?: (sel: string) => Element | null;
  classList?: DOMTokenList;
  dataset?: DOMStringMap;
  id?: string;
  tagName?: string;
  nodeType?: number;
  remove?: () => void;
  alt?: string;
  src?: string;
  currentSrc?: string;
  naturalWidth?: number;
  naturalHeight?: number;
}
