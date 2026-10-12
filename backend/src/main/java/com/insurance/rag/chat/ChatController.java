package com.insurance.rag.chat;

import com.insurance.rag.domain.Conversation;
import jakarta.validation.constraints.NotBlank;
import java.util.List;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/** 问答与历史接口（§7.2）。 */
@RestController
@RequestMapping("/api/chat")
public class ChatController {

    private final ChatService chatService;

    public ChatController(ChatService chatService) {
        this.chatService = chatService;
    }

    public record ChatRequest(@NotBlank String question) {
    }

    /**
     * SSE 流式问答。
     *
     * <p>事件序列：{@code delta}（正文增量）→ {@code citations}（引用）→ {@code done}（收尾）。
     * 出参被合规拦截时，{@code done} 里的 answer 会被替换为拦截提示。
     */
    @PostMapping(value = "/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter stream(@RequestBody ChatRequest req) {
        SseEmitter emitter = new SseEmitter(180_000L);
        chatService.chat(req.question(), emitter);
        return emitter;
    }

    /** 阻塞式问答（§7.2）。FR-09 评测脚本走这个端点，保证评测的是用户实际可见的行为。 */
    @PostMapping
    public ChatService.ChatResponse chat(@RequestBody ChatRequest req) {
        return chatService.chatBlocking(req.question());
    }

    /** 会话历史（FR-07）。 */
    @GetMapping("/history")
    public List<Conversation> history() {
        return chatService.history();
    }
}