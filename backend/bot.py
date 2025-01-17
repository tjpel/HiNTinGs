from dotenv import load_dotenv
from langchain import LLMMathChain, SerpAPIWrapper
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.vectorstores import Chroma
from langchain.text_splitter import CharacterTextSplitter
from langchain.chains import RetrievalQA
from langchain.document_loaders import DirectoryLoader
from langchain.agents import initialize_agent, Tool
from langchain.agents import AgentType
from langchain.chat_models import ChatOpenAI
from langchain.schema import HumanMessage
from nemoguardrails import LLMRails, RailsConfig
from nemoguardrails.actions import action, fact_checking, hallucination
from gradio_tools.tools import StableDiffusionTool


# load environmental variables
load_dotenv()


class Bot:
    def __init__(self, files_path: str, config_path: str = "config/base"):
        """
        The constructor method for the Bot class takes a file path as input and initializes the class by loading and splitting the text using the TextLoader and CharacterTextSplitter
        classes. It also turns the text into embeddings and creates a Chroma vector database using OpenAIEmbeddings and Chroma. Finally, it sets up the RetrievalQA class using a GPT-3
        language model and the Chroma vector database.

        ### Parameters:
        - `files_path` (str): The file path to the set of documents to be queried.
        - `config_path` (str): The file path to the guardrails config folder. Defaults to "config/base"
        """
        self.files_path = files_path
        self.config_path = config_path

        # set up NeMo rails
        config = RailsConfig.from_path(config_path)
        # initialize NeMo App
        self.app = LLMRails(config)

        # initialize search
        self.search = SerpAPIWrapper()

        # initialize math chain
        self.llm_math_chain = LLMMathChain.from_llm(self.app.llm, verbose=True)
        self.lastSource = None
        self.snippet = None

        # initialize LLM for determining whether web search is needed
        self.llm = ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0)

    @action()
    async def query_base_chain(self, q: str):
        res = self.qa({"query": q})
        answer = res["result"]
        self.lastSource = res["source_documents"][0].metadata["source"]
        self.snippet = res["source_documents"][0].page_content
        return answer

    def query(self, q: str, mode=1) -> str:
        # QA
        if mode == 1:
            return self.app.generate(q)
        # serpapi integration for QA
        if mode == 2:
            return self.run_qa(q)
        # OpenAI functions
        else:
            return self.agent.run(q)

    def load_docs(self):
        self.loader = DirectoryLoader(self.files_path)
        self.documents = self.loader.load()

    # runs QA, if there is no context, we do a web search
    def run_qa(self, q: str):
        hintings = self.app.generate(q)

        prompt = f"""Giving just yes or no as an answer. Answer no if the response states there is not enough
        context, I don't know, no permissions , or a refusal to answer. Otherwise, answer yes.
    
        response: {hintings}"""

        res = self.llm([HumanMessage(content=prompt)]).content

        # if there is no context, we run a web search
        if res.lower() == "no":
            print(
                "There is no answer found in the documents. Here is some information from the web:"
            )
            self.lastSource = "web search, no info from documents"
            return self.search.run(q)
        else:
            print("Found in uploaded documents:")
            return hintings

    def run_serpapi(self, q: str) -> str:
        res = self.search.run(q)
        self.lastSource = "web search"
        return res

    def run_diffusion(self, q: str) -> str:
        path = StableDiffusionTool().langchain.run(q)
        self.lastSource = "stable diffusion"
        return path

    def run_math(self, q: str) -> str:
        res = self.llm_math_chain.run(q)
        self.lastSource = "LLM math"
        return res

    def process_docs(self):
        text_splitter = CharacterTextSplitter(chunk_size=1500, chunk_overlap=0)
        texts = text_splitter.split_documents(self.documents)

        embeddings = OpenAIEmbeddings()
        docsearch = Chroma.from_documents(texts, embeddings)

        # creates the QA chain, should reference to the source
        self.qa = RetrievalQA.from_chain_type(
            llm=self.app.llm,
            chain_type="stuff",
            retriever=docsearch.as_retriever(search_kwargs={"k": 2}),
            return_source_documents=True,
        )

        # allows NeMo app to query our QA system
        self.app.register_action(self.query_base_chain, name="main_chain")
        self.app.register_action(fact_checking, name="fact_checking")
        self.app.register_action(hallucination, name="hallucination")

        tools = [
            Tool(
                name="Search",
                func=self.run_serpapi,
                description="useful for when you need to answer questions about current events. You should ask targeted questions",
            ),
            Tool(
                name="Calculator",
                func=self.run_math,
                description="useful for when you need to answer questions about math",
            ),
            Tool(
                name="QA-System",
                func=self.app.generate,
                description="useful for when asking questions about documents that you have uploaded",
            ),
            Tool(
                name="Diffusion",
                func=self.run_diffusion,
                description="useful for when the user asks to create an image based on a prompt",
            ),
        ]

        self.agent = initialize_agent(
            tools, self.app.llm, agent=AgentType.OPENAI_FUNCTIONS, verbose=True
        )
