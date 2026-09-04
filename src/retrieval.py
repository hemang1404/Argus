"""Semantic Retrieval Engine for Few-Shot Examples and RAG Documentation.

Uses sentence-transformers (all-MiniLM-L6-v2) and cosine similarity to dynamically
retrieve top-K relevant few-shot exemplars and technical documentation chunks.
"""

import json
import os
import time
from typing import List, Dict, Any, Tuple
import numpy as np
from sentence_transformers import SentenceTransformer


class ContextRetriever:
    """Embeds knowledge sources and retrieves relevant context with latency tracking."""

    def __init__(
        self,
        few_shot_path: str = "data/few_shot_examples.jsonl",
        rag_docs_path: str = "data/rag_documents.jsonl",
        model_name: str = "all-MiniLM-L6-v2"
    ):
        print(f"[*] Initializing ContextRetriever with model `{model_name}`...")
        self.model = SentenceTransformer(model_name)
        
        # 1. Load Few-Shot Exemplars & Pre-embed
        self.few_shot_pool: List[Dict[str, Any]] = []
        if os.path.exists(few_shot_path):
            with open(few_shot_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self.few_shot_pool.append(json.loads(line.strip()))
                        
        few_shot_texts = [f"{ex['problem']}\n{ex['solution']}" for ex in self.few_shot_pool]
        if few_shot_texts:
            print(f"[*] Pre-computing embeddings for {len(few_shot_texts)} few-shot exemplars...")
            self.few_shot_embeddings = self.model.encode(few_shot_texts, normalize_embeddings=True)
        else:
            self.few_shot_embeddings = np.array([])

        # 2. Load RAG Documents & Pre-embed
        self.rag_docs: List[Dict[str, Any]] = []
        if os.path.exists(rag_docs_path):
            with open(rag_docs_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self.rag_docs.append(json.loads(line.strip()))
                        
        rag_texts = [f"{doc['topic']}: {doc['content']}" for doc in self.rag_docs]
        if rag_texts:
            print(f"[*] Pre-computing embeddings for {len(rag_texts)} RAG docs...")
            self.rag_embeddings = self.model.encode(rag_texts, normalize_embeddings=True)
        else:
            self.rag_embeddings = np.array([])
            
        print("[SUCCESS] ContextRetriever ready.")

    def retrieve_few_shot(self, query: str, k: int = 2) -> Tuple[List[Dict[str, Any]], float]:
        """Retrieves top-K most semantically similar few-shot examples."""
        if len(self.few_shot_pool) == 0:
            return [], 0.0
            
        start_time = time.perf_counter()
        query_emb = self.model.encode([query], normalize_embeddings=True)[0]
        
        # Cosine similarity is dot product when embeddings are normalized
        similarities = np.dot(self.few_shot_embeddings, query_emb)
        top_indices = np.argsort(similarities)[::-1][:k]
        
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        results = [self.few_shot_pool[idx] for idx in top_indices]
        return results, round(latency_ms, 2)

    def retrieve_rag_docs(self, query: str, k: int = 3) -> Tuple[List[Dict[str, Any]], float]:
        """Retrieves top-K most semantically relevant RAG documentation chunks."""
        if len(self.rag_docs) == 0:
            return [], 0.0
            
        start_time = time.perf_counter()
        query_emb = self.model.encode([query], normalize_embeddings=True)[0]
        
        similarities = np.dot(self.rag_embeddings, query_emb)
        top_indices = np.argsort(similarities)[::-1][:k]
        
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        results = [self.rag_docs[idx] for idx in top_indices]
        return results, round(latency_ms, 2)


if __name__ == "__main__":
    print("=== Testing Context Retriever ===")
    retriever = ContextRetriever()
    
    test_query = "Write a python function to find the maximum sum contiguous subarray."
    
    fs_results, fs_time = retriever.retrieve_few_shot(test_query, k=2)
    print(f"\n[Few-Shot Retrieval ({fs_time} ms)] Top 2 matches:")
    for i, res in enumerate(fs_results, 1):
        print(f"  {i}. ID: {res['id']} | Problem: {res['problem'][:60]}...")
        
    rag_results, rag_time = retriever.retrieve_rag_docs(test_query, k=3)
    print(f"\n[RAG Doc Retrieval ({rag_time} ms)] Top 3 matches:")
    for i, res in enumerate(rag_results, 1):
        print(f"  {i}. Topic: {res['topic']} (Doc: {res['doc_id']})")
