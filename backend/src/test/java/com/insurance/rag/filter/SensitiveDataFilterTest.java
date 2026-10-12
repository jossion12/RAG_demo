package com.insurance.rag.filter;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * FR-08 敏感过滤的验收依据（对应 AC-06）。
 *
 * <p>除「该拦的拦住」外，同样重要的是「不该拦的别拦」：
 * 过滤器若把保险条款里的正常表述（等待期天数、赔付比例）当成证件号，
 * 会直接毁掉问答功能。所以这里把不误伤当成一等验收项。
 */
class SensitiveDataFilterTest {

    private final SensitiveDataFilter filter = new SensitiveDataFilter();

    @Test
    @DisplayName("入参：身份证号被掩码，且不泄露明文")
    void masksIdCard() {
        FilterResult r = filter.filterInput("我的身份证是110101199003078515，请帮我看看条款");
        assertThat(r.blocked()).isFalse();
        assertThat(r.hasMask()).isTrue();
        assertThat(r.text()).doesNotContain("110101199003078515");
        assertThat(r.text()).contains("110***********8515");
    }

    @Test
    @DisplayName("入参：手机号被掩码")
    void masksPhone() {
        FilterResult r = filter.filterInput("我的手机号13812345678");
        assertThat(r.text()).doesNotContain("13812345678");
        assertThat(r.text()).contains("138****5678");
    }

    @Test
    @DisplayName("入参：16~19 位银行卡被掩码")
    void masksBankCard() {
        FilterResult r = filter.filterInput("卡号 6222021234567890123");
        assertThat(r.text()).doesNotContain("6222021234567890123");
        assertThat(r.hasMask()).isTrue();
    }

    @Test
    @DisplayName("18 位数字按身份证优先处理，不被当成银行卡二次掩码")
    void idCardTakesPrecedenceOverBankCard() {
        FilterResult r = filter.filterInput("110101199003078515");
        // 只应被掩码一次：若两条规则都命中，maskedSpans 会出现两个标签
        assertThat(r.maskedSpans()).containsExactly("身份证号");
        assertThat(r.text()).isEqualTo("110***********8515");
    }

    @Test
    @DisplayName("承诺语入参被拦截")
    void blocksForbiddenPhraseInInput() {
        FilterResult r = filter.filterInput("你们这个产品能保证收益吗？");
        assertThat(r.blocked()).isTrue();
        assertThat(r.hits()).contains("保证收益");
    }

    @Test
    @DisplayName("承诺语出参被拦截")
    void blocksForbiddenPhraseInOutput() {
        FilterResult r = filter.filterOutput("该产品可以稳赚不赔，收益有保障。");
        assertThat(r.blocked()).isTrue();
        assertThat(r.hits()).contains("稳赚不赔");
    }

    @Test
    @DisplayName("年化收益率数字被识别为承诺语")
    void blocksConcreteReturnRate() {
        assertThat(filter.filterOutput("预期年化收益率 8%").blocked()).isTrue();
    }

    @Test
    @DisplayName("✅ 不误伤：正常保险提问不得被掩码或拦截")
    void doesNotTouchNormalInsuranceQuestion() {
        String q = "这款住院医疗险的等待期是多少天？一般医疗免赔额是多少钱？";
        FilterResult r = filter.filterInput(q);
        assertThat(r.blocked()).isFalse();
        assertThat(r.hasMask()).isFalse();
        assertThat(r.text()).isEqualTo(q);
    }

    @Test
    @DisplayName("✅ 不误伤：条款中的短数字（天数/比例）不得被当成证件号")
    void doesNotMaskShortNumbers() {
        String q = "等待期 30 日，赔付比例 100%，免赔额 1 万元，保额 600 万";
        FilterResult r = filter.filterInput(q);
        assertThat(r.hasMask()).isFalse();
        assertThat(r.text()).isEqualTo(q);
    }

    @Test
    @DisplayName("✅ 不误伤：只提到「收益」但未承诺，不应拦截")
    void doesNotBlockNeutralMentionOfYield() {
        // 「收益」二字本身不是红线；红线是带承诺的表述
        FilterResult r = filter.filterInput("重疾险有身故保险金吗？投保人豁免能给家人带来什么？");
        assertThat(r.blocked()).isFalse();
    }

    @Test
    @DisplayName("空值与 null 安全")
    void handlesNullAndBlank() {
        assertThat(filter.filterInput(null).text()).isNull();
        assertThat(filter.filterInput("").text()).isEmpty();
        assertThat(filter.filterOutput(null).blocked()).isFalse();
    }

    @Test
    @DisplayName("边界：号码前后紧邻中文时仍须命中（\\b 在中文语境下不可靠）")
    void matchesWhenSurroundedByChinese() {
        // 回归用例：曾用 \b 作为边界时，「我的身份证是110101199003078515」匹配失败，
        // 因为 Java 正则的 \w 只有 [a-zA-Z_0-9]，中文全被算作非单词字符。
        assertThat(filter.filterInput("身份证是110101199003078515谢谢").hasMask()).isTrue();
        assertThat(filter.filterInput("电话13812345678谢谢").hasMask()).isTrue();
        assertThat(filter.filterInput("（13812345678）").hasMask()).isTrue();
    }

    @Test
    @DisplayName("边界：更长数字串中不截取（22 位不应被当成身份证或银行卡）")
    void doesNotMatchInsideLongerNumber() {
        String s = "订单号20261010123456789012345已发货";
        FilterResult r = filter.filterInput(s);
        assertThat(r.hasMask()).isFalse();
    }

    @Test
    @DisplayName("掩码规则：短值整体打码，长值保留首尾")
    void maskShape() {
        assertThat(SensitiveDataFilter.mask("1234567")).isEqualTo("*******");
        assertThat(SensitiveDataFilter.mask("110101199003078515")).isEqualTo("110***********8515");
    }
}