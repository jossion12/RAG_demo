package com.insurance.rag.dify;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.insurance.rag.config.DifyProperties;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Flux;

/**
 * Dify Chatflow 客户端。
 *
 * <p>关于端点：<b>Chatflow 应用同样用 {@code /v1/chat-messages}</b>，
 * Dify 不会因为应用是 Chatflow（{@code advanced-chat}）就换一个路径。
 * 实测 Dify 1.17.1：
 * <pre>
 *   POST /v1/chatflow/chat-messages -&gt; 404 Not Found
 *   POST /v1/chat-messages          -&gt; 401 unauthorized（仅缺 Key，说明路由存在）
 * </pre>
 * 路由定义见 api/controllers/service_api/app/completion.py:324。
 * service_api 下与「聊天」相关的路由只有 {@code /chat-messages}
 * 和 {@code /chat-messages/<task_id>/stop}，没有 {@code /chatflow/*} 这一族。
 *
 * <p>应用类型（chat / advanced-chat / workflow）由 Key 对应的 app 决定，
 * 不体现在 URL 上。Workflow 应用才用 {@code /v1/workflows/run}。
 */
@Component
public class DifyClient {

    private static final Logger log = LoggerFactory.getLogger(DifyClient.class);

    private final WebClient webClient;
    private final DifyProperties props;
    private final ObjectMapper mapper;

    public DifyClient(WebClient.Builder builder, DifyProperties props, ObjectMapper mapper) {
        this.props = props;
        this.mapper = mapper;
        this.webClient = builder
                .baseUrl(props.getBaseUrl())
                .defaultHeader("Authorization", "Bearer " + props.getAppApiKey())
                .build();
    }

    /** 流式事件：文本增量或最终汇总。 */
    public sealed interface ChatChunk {
        record Delta(String text) implements ChatChunk {}

        record Finished(DifyEvents.ChatResult result) implements ChatChunk {}

        record Failed(String code, String message) implements ChatChunk {}
    }

    /**
     * 以 SSE 方式发起问答。
     *
     * <p>Dify 的流式事件里，正文在 {@code message} 事件的 {@code answer} 字段；
     * 引用来源（retriever_resources）与 token 用量在 {@code message_end} 的 {@code metadata} 里。
     * 两者必须同时收集——只收正文就丢掉溯源。
     */
    public Flux<ChatChunk> streamChat(String query, String conversationId) {
        long start = System.currentTimeMillis();
        StringBuilder answer = new StringBuilder();
        String[] convId = {conversationId == null ? "" : conversationId};

        ChatRequest body = new ChatRequest(query, Map.of(), convId[0], "streaming", props.getEndUser());

        return webClient.post()
                .uri("/v1/chat-messages")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .bodyValue(body)
                .retrieve()
                .bodyToFlux(DifyEvents.Event.class)
                .concatMap(event -> {
                    if (event.conversation_id != null) {
                        convId[0] = event.conversation_id;
                    }
                    return switch (event.event == null ? "" : event.event) {
                        case "message" -> {
                            if (event.answer != null && !event.answer.isEmpty()) {
                                answer.append(event.answer);
                                yield Flux.<ChatChunk>just(new ChatChunk.Delta(event.answer));
                            }
                            yield Flux.empty();
                        }
                        case "message_end" -> {
                            var meta = event.metadata;
                            List<DifyEvents.RetrieverResource> resources =
                                    meta == null ? List.of() : meta.resources();
                            var usage = meta == null ? null : meta.usage;
                            boolean refused = resources.isEmpty();
                            yield Flux.<ChatChunk>just(new ChatChunk.Finished(new DifyEvents.ChatResult(
                                    answer.toString(), convId[0], resources, usage,
                                    refused, System.currentTimeMillis() - start)));
                        }
                        case "error" -> Flux.<ChatChunk>just(new ChatChunk.Failed(
                                event.code == null ? "unknown" : event.code,
                                event.message == null ? "Dify 返回错误但无 message" : event.message));
                        default -> Flux.empty();
                    };
                })
                .onErrorResume(ex -> {
                    log.warn("Dify 调用失败: {}", ex.toString());
                    return Flux.<ChatChunk>just(new ChatChunk.Failed("upstream_error", ex.getMessage()));
                });
    }

    /** 阻塞模式，供评测脚本使用（FR-09 批量跑评测集不需要流式）。 */
    public DifyEvents.ChatResult chatBlocking(String query) {
        long start = System.currentTimeMillis();
        ChatRequest body = new ChatRequest(query, Map.of(), "", "blocking", props.getEndUser());
        DifyEvents.Event ev = webClient.post()
                .uri("/v1/chat-messages")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(body)
                .retrieve()
                .bodyToMono(DifyEvents.Event.class)
                .block(Duration.ofMillis(props.getReadTimeoutMs()));
        if (ev == null) {
            throw new IllegalStateException("Dify 返回空响应");
        }
        var meta = ev.metadata;
        List<DifyEvents.RetrieverResource> resources = meta == null ? List.of() : meta.resources();
        return new DifyEvents.ChatResult(
                ev.answer == null ? "" : ev.answer,
                ev.conversation_id,
                resources,
                meta == null ? null : meta.usage,
                resources.isEmpty(),
                System.currentTimeMillis() - start);
    }

    /** Chatflow 目前没有必填输入变量，但 inputs 字段必须存在，否则 Dify 返回参数错误。 */
    private record ChatRequest(String query, Map<String, Object> inputs, String conversation_id,
                               String response_mode, String user) {
    }
}