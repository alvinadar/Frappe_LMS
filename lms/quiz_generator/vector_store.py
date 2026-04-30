import os
import frappe
import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings


def _get_embeddings():
    """Lazy-init Google embedding client. Reads gemini_api_key from site config."""
    api_key = (
        frappe.conf.get('gemini_api_key')
        or frappe.conf.get('gemini_api_key_2')
    )
    if not api_key:
        raise RuntimeError('No gemini_api_key configured in site_config.json')
    return GoogleGenerativeAIEmbeddings(
        model='models/text-embedding-004',
        google_api_key=api_key,
    )


def get_chroma_client():
    """Initializes ChromaDB and stores data securely in the site private folder."""
    site_path = frappe.get_site_path()
    db_path = os.path.join(site_path, 'private', 'chromadb')
    if not os.path.exists(db_path):
        os.makedirs(db_path)
    client = chromadb.PersistentClient(path=db_path)
    return client


def get_or_create_collection(collection_name='curriculum_materials'):
    """Fetches or creates the collection where curriculum vectors will live."""
    client = get_chroma_client()
    return client.get_or_create_collection(name=collection_name)


def process_and_store_document(lesson_name, text_content, metadata):
    """Chunks the document, generates embeddings, and stores them in ChromaDB."""
    if not text_content or len(text_content.strip()) == 0:
        return False

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len,
        is_separator_regex=False,
    )
    chunks = text_splitter.split_text(text_content)

    collection = get_or_create_collection()
    embeddings = _get_embeddings()

    ids = []
    metadatas = []
    documents = []

    for i, chunk in enumerate(chunks):
        chunk_id = f'{lesson_name}_chunk_{i}'
        ids.append(chunk_id)
        documents.append(chunk)
        chunk_metadata = metadata.copy()
        chunk_metadata['chunk_index'] = i
        chunk_metadata['lesson_name'] = lesson_name
        metadatas.append(chunk_metadata)

    embedded_vectors = embeddings.embed_documents(documents)

    collection.upsert(
        ids=ids,
        embeddings=embedded_vectors,
        metadatas=metadatas,
        documents=documents,
    )

    frappe.logger().info(f'[VectorStore] Stored {len(chunks)} chunks for {lesson_name}')
    return True


def search_similar_chunks(query_text, n_results=5, filter_metadata=None):
    """Retrieves the most relevant chunks based on semantic similarity."""
    collection = get_or_create_collection()
    embeddings = _get_embeddings()
    query_embedding = embeddings.embed_query(query_text)

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        where=filter_metadata,
    )
    return results
