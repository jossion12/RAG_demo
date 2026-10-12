package com.insurance.rag;

import com.insurance.rag.config.DifyProperties;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

@SpringBootApplication
@EnableConfigurationProperties(DifyProperties.class)
public class InsuranceRagApplication {

    public static void main(String[] args) {
        SpringApplication.run(InsuranceRagApplication.class, args);
    }
}