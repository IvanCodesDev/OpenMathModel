export type KnowledgeProblem = {
  id: string;
  code: string;
  title: string;
  competition: string;
  category: string;
  year: number;
  problem_type: string;
  modeling_directions: string[];
  keywords: string[];
  data_requirement: string;
  status: string;
  [key: string]: unknown;
};

export type KnowledgePaper = {
  title: string;
  problem_id?: string;
  problem_code: string;
  competition: string;
  category: string;
  year: number;
  award: string;
  institution?: string;
  distinctions: string[];
  models: string[];
  innovation: string;
  [key: string]: unknown;
};

export type KnowledgeLibrary = {
  schema_version: string;
  dataset_version: string;
  generated_at: string;
  stats: Record<string, unknown>;
  problems: KnowledgeProblem[];
  papers: KnowledgePaper[];
};
