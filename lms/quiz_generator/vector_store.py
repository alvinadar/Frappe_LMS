import os
import frappe
import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings

# Initialize the embedding model specified in your architecture
# This will download the model the first time it runs
embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

def get_chroma_client():
    """Initializes ChromaDB and stores data securely in the site's private folder."""
    site_path = frappe.get_site_path()
    db_path = os.path.join(site_path, 'private', 'chromadb')
    
    # Create the directory if it doesn't exist
    if not os.path.exists(db_path):
        os.makedirs(db_path)
        
    client = chromadb.PersistentClient(path=db_path)
    return client

def get_or_create_collection(collection_name="curriculum_materials"):
    """Fetches or creates the collection where curriculum vectors will live."""
    client = get_chroma_client()
    return client.get_or_create_collection(name=collection_name)

def process_and_store_document(lesson_name, text_content, metadata):
    """Chunks the document, generates embeddings, and stores them in ChromaDB."""
    if not text_content or len(text_content.strip()) == 0:
        return False

    # Chunking: Split text into overlapping segments
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len,
        is_separator_regex=False,
    )
    chunks = text_splitter.split_text(text_content)

    collection = get_or_create_collection()
    
    ids = []
    metadatas = []
    documents = []
    
    for i, chunk in enumerate(chunks):
        chunk_id = f"{lesson_name}_chunk_{i}"
        ids.append(chunk_id)
        documents.append(chunk)
        
        # Add chunk index to the base metadata
        chunk_metadata = metadata.copy()
        chunk_metadata["chunk_index"] = i
        chunk_metadata["lesson_name"] = lesson_name
        metadatas.append(chunk_metadata)

    # Compute embeddings using Sentence-Transformers
    embedded_vectors = embeddings.embed_documents(documents)

    # Store chunks, embeddings, and metadata in ChromaDB
    collection.upsert(
        ids=ids,
        embeddings=embedded_vectors,
        metadatas=metadatas,
        documents=documents
    )
    
    frappe.logger().info(f"[VectorStore] Successfully stored {len(chunks)} chunks for {lesson_name}")
    return True

def search_similar_chunks(query_text, n_results=5, filter_metadata=None):
    """Retrieves the most relevant chunks based on semantic similarity."""
    collection = get_or_create_collection()
    query_embedding = embeddings.embed_query(query_text)
    
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        where=filter_metadata # Optional: Filter by subject, difficulty, etc.
    )
    
    return results
