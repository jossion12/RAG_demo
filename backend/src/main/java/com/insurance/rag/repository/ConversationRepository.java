package com.insurance.rag.repository;

import com.insurance.rag.domain.Conversation;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ConversationRepository extends JpaRepository<Conversation, String> {

    /** 会话历史，按时间倒序（FR-07）。 */
    List<Conversation> findTop50ByOrderByCreatedAtDesc();
}