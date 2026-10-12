package com.insurance.rag.chat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.insurance.rag.citation.Citation;
import com.insurance.rag.citation.CitationAssembler;
import com.insurance.rag.dify.DifyClient;
import com.insurance.rag.dify.DifyEvents;
import com.insurance.rag.domain.Conversation;
import com.insurance.rag.filter.FilterResult;
import com.insurance.rag.filter.SensitiveDataFilter;
import com.insurance.rag.repository.ConversationRepository;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * 问答编排（FR-04 / FR-05 / FR-06 / FR-07 / FR-08 的交汇点）。
 *
 * <p>处理顺序是刻意的，每一步对应一条需求：
 * <ol>
 *   <li>入参过滤 —— 必须在转发给 Dify <b>之前</b>，否则明文已经出网；</li>
 *   <li>转发 Dify 并把正文增量实时吐给前端；</li>
 *   <li>出参过滤 —— 承诺语拦截在<b>累积完</b>之后做，因为流式过程中无法判断整句；</li>
 *   <li>组装引用、落库。</li>
 * </ol>
 */
@Service
public class ChatService {

    private static final Logger log = LoggerFactory.getLogger(ChatService.class);

    private final DifyClient dify;
    private final SensitiveDataFilter filter;
    private final CitationAssembler assembler;
    private final ConversationRepository repository;
    private final ObjectMapper mapper;

    public ChatService(DifyClient dify, SensitiveDataFilter filter, CitationAssembler assembler,
                       ConversationRepository repository, ObjectMapper mapper) {
        this.dify = dify;
        this.filter = filter;
        this.assembler = assembler;
        this.repository = repository;
        this.mapper = mapper;
    }

    /** 处理一次问答，边转发边落库。 */
    public void chat(String rawQuestion, SseEmitter emitter) {
        String requestId = UUID.randomUUID().toString();

        // 1) 入参过滤
        FilterResult in = filter.filterInput(rawQuestion);
        if (in.blocked()) {
            log.info("入参命中敏感承诺语，已拦截 hits={} requestId={}", in.hits(), requestId);
            send(emitter, "blocked", json(Map.of(
                    "message", "该问题涉及收益承诺等合规红线，无法回答。",
                    "hits", in.hits())));
            emitter.complete();
            audit(requestId, rawQuestion, null, in, true, 0L, null, null);
            return;
        }
        String question = in.text();

        // 2) 调 Dify
        final StringBuilder answer = new StringBuilder();
        dify.streamChat(question, null)
                .subscribe(
                        chunk -> {
                            if (chunk instanceof DifyClient.ChatChunk.Delta d) {
                                answer.append(d.text());
                                send(emitter, "delta", d.text());
                            } else if (chunk instanceof DifyClient.ChatChunk.Finished f) {
                                finish(requestId, question, answer.toString(), f.result(), in, emitter);
                            } else if (chunk instanceof DifyClient.ChatChunk.Failed x) {
                                send(emitter, "error", json(Map.of(
                                        "code", x.code(), "message", x.message())));
                                emitter.complete();
                            }
                        },
                        ex -> {
                            log.error("问答异常 requestId={}", requestId, ex);
                            send(emitter, "error", json(Map.of(
                                    "code", "internal", "message", String.valueOf(ex.getMessage()))));
                            emitter.complete();
                        });
    }

    private void finish(String requestId, String question, String answer,
                        DifyEvents.ChatResult result, FilterResult in, SseEmitter emitter) {
        // 3) 出参过滤
        FilterResult out = filter.filterOutput(answer);
        List<Citation> citations = assembler.assemble(result.resources());
        List<Double> scores = result.resources().stream()
                .map(r -> r.score).collect(Collectors.toList());

        try {
            String citationsJson = json(citations);
            String scoresJson = json(scores);
            send(emitter, "citations", citationsJson);
            send(emitter, "done", json(Map.of(
                    "answer", out.blocked() ? "[已拦截：输出包含合规红线表述]" : answer,
                    "refused", result.refused(),
                    "elapsedMs", result.elapsedMs(),
                    "conversationId", result.conversationId() == null ? "" : result.conversationId())));
        } catch (Exception e) {
            log.warn("序列化失败", e);
        } finally {
            emitter.complete();
        }

        // 4) 落库
        audit(requestId, question, answer, in, result.refused(), result.elapsedMs(), citations, scores);
    }

    /**
     * 序列化助手：SSE 推给前端的内容序列化失败不应中断业务，
     * 退化成空对象即可——比让受检异常层层上抛更符合这里的使用场景。
     */
    private String json(Object value) {
        try {
            return mapper.writeValueAsString(value);
        } catch (Exception e) {
            log.warn("序列化失败，退化为空对象: {}", e.toString());
            return "{}";
        }
    }


    private void audit(String requestId, String question, String answer, FilterResult in,
                       boolean refused, Long elapsed, List<Citation> citations, List<Double> scores) {
        try {
            Conversation c = new Conversation();
            c.setId(requestId);
            c.setQuestion(question);
            c.setAnswer(answer);
            c.setRefused(refused);
            c.setElapsedMs(elapsed);
            c.setCreatedAt(Instant.now());
            if (!in.maskedSpans().isEmpty()) {
                c.setMaskedLabels(String.join(",", in.maskedSpans()));
            }
            if (citations != null) {
                c.setCitations(json(citations));
                c.setScores(json(scores));
            }
            repository.save(c);
        } catch (Exception e) {
            // 落库失败不应影响用户已收到的答案，只记日志
            log.warn("落库失败 requestId={}: {}", requestId, e.toString());
        }
    }

    /**
     * 阻塞式问答（供 FR-09 评测脚本与前端非流式场景使用）。
     *
     * <p>与流式路径共用同一套过滤与组装逻辑——评测必须测「用户实际看到的行为」，
     * 如果评测另走一条捷径，入参过滤和引用组装就脱离验收了。
     */
    public ChatResponse chatBlocking(String rawQuestion) {
        String requestId = UUID.randomUUID().toString();

        FilterResult in = filter.filterInput(rawQuestion);
        if (in.blocked()) {
            log.info("入参命中敏感承诺语，已拦截 hits={} requestId={}", in.hits(), requestId);
            audit(requestId, rawQuestion, null, in, true, 0L, List.of(), List.of());
            return new ChatResponse("[已拦截：问题涉及合规红线]", List.of(), true, 0L,
                    List.copyOf(in.hits()));
        }

        // Dify 不可用（未配 API Key、服务未起、网络不通）时也必须落审计并降级，
        // 否则审计链路会在故障时静默失效——恰恰是最需要留痕的时候没有留痕。
        DifyEvents.ChatResult result;
        try {
            result = dify.chatBlocking(in.text());
        } catch (Exception e) {
            log.warn("Dify 调用失败，降级返回: {}", e.toString());
            audit(requestId, in.text(), null, in, true, 0L, List.of(), List.of());
            return new ChatResponse(
                    "检索服务暂时不可用，无法作答。请稍后重试。", List.of(), true, 0L, List.of());
        }

        FilterResult out = filter.filterOutput(result.answer());
        List<Citation> citations = assembler.assemble(result.resources());
        List<Double> scores = result.resources().stream().map(r -> r.score).collect(Collectors.toList());

        audit(requestId, in.text(), result.answer(), in, result.refused(),
                result.elapsedMs(), citations, scores);

        return new ChatResponse(
                out.blocked() ? "[已拦截：输出包含合规红线表述]" : result.answer(),
                citations, result.refused(), result.elapsedMs(), List.of());
    }

    /** 问答结果。 */
    public record ChatResponse(String answer, List<Citation> citations, boolean refused,
                               Long elapsedMs, List<String> blockedBy) {
    }

    private void send(SseEmitter emitter, String event, Object payload) {
        try {
            emitter.send(SseEmitter.event().name(event).data(payload == null ? "" : payload));
        } catch (Exception e) {
            log.debug("SSE 推送失败（客户端可能已断开）: {}", e.toString());
        }
    }

    /** 会话历史（FR-07）。 */
    public List<Conversation> history() {
        return repository.findTop50ByOrderByCreatedAtDesc();
    }

}