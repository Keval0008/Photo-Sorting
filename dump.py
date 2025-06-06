from sqllineage.runner import LineageRunner
import pandas as pd
import re
import sqlglot
from sqlglot import parse_one, exp
from rapidfuzz import fuzz
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Union

# Cache parsed SQL to avoid redundant parsing
@lru_cache(maxsize=100)
def parse_sql_cached(sql: str) -> exp.Expression:
    return parse_one(sql, read='bigquery')

def format_and_clean_sql(sql: str) -> str:
    """Formats and cleans SQL query for better readability and consistency."""
    try:
        parsed = parse_sql_cached(sql)
        formatted_sql = parsed.sql(pretty=True)
        return re.sub(r'/\*.*?\*/', ' ', formatted_sql, flags=re.DOTALL)
    except Exception as e:
        print(f"Warning: Formatting failed - {str(e)}")
        return sql

def normalize_sql(sql: str) -> List[str]:
    """Normalizes SQL query by removing extra spaces and converting to lowercase."""
    return [re.sub(r'\s+', ' ', line.strip()).lower() 
            for line in sql.split('\n') if line.strip()]

def get_main_table(ast: exp.Expression) -> Optional[str]:
    """Extracts the main source table from a SQL query."""
    try:
        # Handle WITH clauses (CTEs)
        cte_map = {}
        if ast.args.get("with"):
            for cte in ast.args["with"].expressions:
                cte_map[cte.alias] = cte.this

        # Get the main SELECT's FROM clause
        main_select = ast.find(exp.Select)
        if not main_select or not main_select.args.get("from"):
            return None

        main_from = main_select.args["from"]
        first_source = (main_from.args["expressions"][0] 
                       if main_from.args.get("expressions") 
                       else main_from.this)

        # Drill down through subqueries and aliases
        while True:
            if isinstance(first_source, exp.Subquery):
                first_source = first_source.this.args.get("from", first_source.this).this
            elif isinstance(first_source, exp.Alias):
                first_source = first_source.this
            elif isinstance(first_source, exp.Table):
                if first_source.name in cte_map:
                    first_source = cte_map[first_source.name].args.get("from", cte_map[first_source.name]).this
                else:
                    break
            else:
                break

        return first_source.sql() if isinstance(first_source, exp.Table) else None
    except Exception as e:
        print(f"Warning: Table extraction failed - {str(e)}")
        return None

def get_cte_schemas(ast: exp.Expression) -> Dict:
    """Returns CTE schemas with column definitions and source references."""
    if not ast.args.get("with"):
        return {}

    cte_schemas = {}
    cte_names = {cte.alias for cte in ast.args["with"].expressions}
    
    for cte in ast.args["with"].expressions:
        cte_name = cte.alias
        select = cte.this
        
        # Get column definitions
        columns = {}
        for i, expr in enumerate(select.args["expressions"]):
            if isinstance(expr, exp.Alias):
                col_name = expr.alias
                col_expr = expr.this.sql()
            else:
                col_name = expr.sql() if isinstance(expr, (exp.Column, exp.Star)) else f"col_{i+1}"
                col_expr = expr.sql()
            columns[col_name] = col_expr
        
        # Get source references
        source_tables = set()
        source_ctes = set()
        
        for table in select.find_all(exp.Table):
            table_name = table.sql()
            if table.name in cte_names:
                source_ctes.add(table.name)
            else:
                source_tables.add(table_name)
        
        cte_schemas[cte_name] = {
            "columns": columns,
            "source_tables": sorted(source_tables),
            "source_ctes": sorted(source_ctes)
        }
    
    return cte_schemas

def expand_all_stars(ast: exp.Expression, cte_schemas: Dict = None, debug: bool = False) -> exp.Expression:
    """Multi-pass star expansion that handles CTEs and main query recursively."""
    cte_schemas = cte_schemas or get_cte_schemas(ast)
    max_passes = 5
    
    for pass_num in range(max_passes):
        if debug:
            print(f"\n=== Starting expansion pass {pass_num + 1} ===")
        
        # Check for remaining stars
        has_stars = any(
            isinstance(e, (exp.Star, exp.Column)) 
            for select in ast.find_all(exp.Select) 
            for e in select.args.get("expressions", [])
        )
        
        if not has_stars:
            if debug:
                print("No more stars to expand")
            break
        
        # Expand CTEs first (bottom-up)
        if ast.args.get("with"):
            for cte in ast.args["with"].expressions:
                if debug:
                    print(f"\nExpanding stars in CTE: {cte.alias}")
                expand_select_star(cte.this, cte_schemas, debug)
        
        # Update CTE schemas after expansion
        cte_schemas = get_cte_schemas(ast)
        
        # Expand main query
        main_select = ast.find(exp.Select)
        if main_select:
            if debug:
                print("\nExpanding stars in main query")
            expand_select_star(main_select, cte_schemas, debug)
    else:
        if debug:
            print("Warning: Reached maximum expansion passes")
    
    return ast

def expand_select_star(select: exp.Select, cte_schemas: Dict, debug: bool = False):
    """Expands stars in a single SELECT statement."""
    if debug:
        print("\nProcessing SELECT:", select.sql())
    
    from_source = select.args.get("from")
    from_tables = get_source_tables(from_source, cte_schemas) if from_source else []
    
    join_tables = []
    for join in select.args.get("joins", []):
        join_tables.extend(get_source_tables(join.this, cte_schemas))
    
    all_sources = from_tables + join_tables
    if debug:
        print(f"Available sources: {all_source_names(all_sources)}")
    
    new_exprs = []
    for expr in select.args.get("expressions", []):
        if isinstance(expr, exp.Star):
            if debug:
                print("Processing unqualified *")
            handle_unqualified_star(expr, all_sources, cte_schemas, new_exprs, debug)
        elif isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            if debug:
                print(f"Processing qualified {expr.sql()}")
            handle_qualified_star(expr, cte_schemas, new_exprs, debug)
        else:
            if debug:
                print(f"Keeping existing expression: {expr.sql()}")
            new_exprs.append(expr)
    
    select.set("expressions", new_exprs)
    if debug:
        print("New expressions:", [e.sql() for e in new_exprs])

def wrap_select_with_insert(sql: str, replacements: Dict, filename: Optional[str] = None)
    """Wraps the final SELECT with an INSERT statement."""
    try:
        # Apply replacements first
        for old, new in replacements.items():
            sql = sql.replace(old, new)
        
        ast = parse_sql_cached(sql)
        main_select = ast.find(exp.Select)
        
        if not main_select:
            raise ValueError("No SELECT found in query")
            
        # Generate column list
        output_columns = []
        for i, expr in enumerate(main_select.expressions):
            if isinstance(expr, exp.Alias):
                output_columns.append(expr.alias)
            else:
                output_columns.append(expr.sql() if not isinstance(expr, exp.Star) else f"col_{i+1}")
        
        if filename:
            # Remove .sql extension and any invalid characters
            target_table = os.path.splitext(filename)[0]
            target_table = re.sub(r'[^a-zA-Z0-9_]', '_', target_table)  # Replace special chars with _
        else:
            # Fallback to original behavior if no filename provided
            from_table = get_main_table(ast) or "unknown"
            target_table = f"dummy_{from_table.split('.')[-1]}"
        
        # Build final SQL
        insert_stmt = f"INSERT INTO {target_table} ({', '.join(output_columns)})"
        return f"{insert_stmt}\n{ast.sql()}"
        
    except Exception as e:
        print(f"Warning: Couldn't wrap query - {str(e)}")
        return sql  # Fallback to original

def clean_lineage_tuple(variable: List[str]) -> List[str]:
    """Cleans lineage tuples by removing default schema references."""
    return [v.replace('<default>.', '') for v in variable]

def extract_column_lineage(sql: str, replacements: Dict, filename: Optional[str] = None, modify: bool = True) -> pd.DataFrame:
    """Extracts column lineage from SQL query."""
    try:
        if modify:
            sql = wrap_select_with_insert(sql, replacements, filename)
        
        # Parse and expand stars
        ast = parse_sql_cached(sql)
        expanded_ast = expand_all_stars(ast)
        processed_sql = expanded_ast.sql()
        
        # Extract lineage
        lineage_mapping = []
        for lineage_tuple in LineageRunner(processed_sql).get_column_lineage():
            try:
                chain = [str(col) for col in lineage_tuple]
                cleaned_chain = clean_lineage_tuple(chain)
                
                for i in range(len(cleaned_chain) - 1):
                    target = cleaned_chain[i]
                    source = cleaned_chain[i + 1]
                    
                    lineage_mapping.append((
                        target if len(target.split('.')) == 2 else f"<unknown>.{target}",
                        source if len(source.split('.')) == 2 else f"<unknown>.{source}"
                    ))
            except Exception as e:
                print(f"Skipping lineage tuple due to error: {str(e)}")
                continue
        
        # Create DataFrame
        lineage_df = pd.DataFrame(lineage_mapping, columns=["derived", "source"])
        lineage_df[['derived_table', 'derived_column']] = lineage_df['derived'].str.split('.', expand=True)
        lineage_df[['source_table', 'source_column']] = lineage_df['source'].str.split('.', expand=True)
        
        return lineage_df[['source_table', 'source_column', 'derived_table', 'derived_column']].drop_duplicates()
    
    except Exception as e:
        print(f"Lineage extraction failed: {str(e)}")
        return pd.DataFrame()

def process_sql_file(file_path: str, replacements: Dict) -> pd.DataFrame:
    """Processes a single SQL file and returns lineage DataFrame."""
    try:
        with open(file_path, 'r') as f:
            sql = f.read().strip()
            if not sql:
                print(f"Skipping empty file: {file_path}")
                return pd.DataFrame()
            
            # Get just the filename without path
            filename = os.path.basename(file_path)
            return extract_column_lineage(sql, replacements, filename)
    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}")
        return pd.DataFrame()

def process_sql_folder(folder_path: str, replacements: Dict) -> pd.DataFrame:
    """Processes all SQL files in a folder and returns combined lineage."""
    sql_files = [
        os.path.join(folder_path, f) 
        for f in os.listdir(folder_path) 
        if f.endswith('.sql')
    ]
    
    # Process files in parallel
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(
            lambda f: process_sql_file(f, replacements), 
            sql_files
        ))
    
    return pd.concat(results).drop_duplicates().reset_index(drop=True)
from sqllineage.runner import LineageRunner
import pandas as pd
import re
import sqlglot
from sqlglot import parse_one, exp
from rapidfuzz import fuzz
import os

def format_and_clean_sql(sql):
    """Formats and cleans SQL query for better readability and consistency.
    Args:
        sql (str): SQL query string.
    Returns:
        str: Formatted SQL query string.
    """
    parsed = parse_one(sql, read='bigquery')
    formatted_sql = parsed.sql(pretty=True)
    formatted_sql = re.sub(r'/\*.*?\*/', ' ', formatted_sql, flags=re.DOTALL)  # Remove comments
    return formatted_sql

def normalize_sql(sql):
    """Normalizes SQL query by removing extra spaces and converting to lowercase.
    Args:
        sql (str): SQL query string.
    Returns:
        list: List of normalized SQL lines.
        """
    return [re.sub(r'\s+', ' ', line.strip()).lower() for line in sql if line.strip()]


from sqlglot import parse_one, exp

def get_main_table(ast):
    """
    Extracts the main source table from a SQL query, handling:
    - Simple FROM clauses
    - Subqueries
    - CTEs
    - Complex joins
    """
    # Handle WITH clauses (CTEs) if they exist
    cte_map = {}
    if ast.args.get("with"):
        for cte in ast.args["with"].expressions:
            cte_map[cte.alias] = cte.this

    # Get the main SELECT's FROM clause
    main_select = ast.find(exp.Select)
    if not main_select or not main_select.args.get("from"):
        return None

    main_from = main_select.args["from"]
    
    # Get the first source in FROM (could be a table, subquery, or join)
    first_source = main_from.args["expressions"][0] if main_from.args.get("expressions") else main_from.this
    
    # Drill down through subqueries and aliases
    while True:
        if isinstance(first_source, exp.Subquery):
            # Handle subqueries: get the actual query inside
            first_source = first_source.this.args.get("from", first_source.this).this
        elif isinstance(first_source, exp.Alias):
            # Handle table aliases
            first_source = first_source.this
        elif isinstance(first_source, exp.Table):
            # Handle CTE references
            if first_source.name in cte_map:
                first_source = cte_map[first_source.name].args.get("from", cte_map[first_source.name]).this
            else:
                break
        else:
            break
    
    return first_source.sql() if isinstance(first_source, exp.Table) else None


for sql in queries:
    ast = parse_one(sql, read="bigquery")
    main_table = get_main_table(ast)
    # print(f"Query: {sql}")
    print(f"Main table: {main_table}\n")

from sqlglot import parse_one, exp

def get_enhanced_cte_details(ast):
    if not ast.args.get("with"):
        return {}

    cte_details = {}
    
    for cte in ast.args["with"].expressions:
        cte_name = cte.alias
        cte_def = cte.this
        
        # Get all tables and their usage context
        table_refs = []
        for table in cte_def.find_all(exp.Table):
            context = None
            parent = table.parent
            
            # Determine the context (FROM, JOIN, subquery, etc.)
            while parent:
                if isinstance(parent, exp.From):
                    context = "FROM"
                    break
                elif isinstance(parent, exp.Join):
                    context = "JOIN"
                    break
                elif isinstance(parent, exp.Subquery):
                    context = "SUBQUERY"
                    break
                parent = parent.parent
            
            table_refs.append({
                'table_name': table.sql(),
                'context': context,
                'is_cte_ref': table.name in cte_details
            })
        
        cte_details[cte_name] = {
            'definition': cte_def.sql(),
            'table_references': table_refs,
            'is_recursive': any(ref['is_cte_ref'] and ref['table_name'] == cte_name 
                           for ref in table_refs)
        }
    
    return cte_details

ast = parse_one(sql, read="bigquery")
cte_info = get_enhanced_cte_details(ast)

for cte_name, details in cte_info.items():
    print(f"CTE: {cte_name}")
    print(f"Definition: {details['definition']}")
    print(f"Source Tables: {details['table_references']}")
    print(f"Recursive: {details['is_recursive']}")

from sqlglot import parse_one, exp

def get_cte_schemas(ast):
    """
    Returns a dictionary mapping each CTE name to its schema, including:
    - Column definitions (name: source expression)
    - Source tables (both physical tables and referenced CTEs)
    """
    if not ast.args.get("with"):
        return {}

    cte_schemas = {}
    
    # First pass to collect all CTE names
    cte_names = {cte.alias for cte in ast.args["with"].expressions}
    
    for cte in ast.args["with"].expressions:
        cte_name = cte.alias
        select = cte.this
        
        # Get column definitions
        columns = {}
        for i, expr in enumerate(select.args["expressions"]):
            if isinstance(expr, exp.Alias):
                col_name = expr.alias
                col_expr = expr.this.sql()
            else:
                col_name = expr.sql() if isinstance(expr, (exp.Column, exp.Star)) else f"col_{i+1}"
                col_expr = expr.sql()
            columns[col_name] = col_expr
        
        # Get all source references
        source_tables = set()
        source_ctes = set()
        
        for table in select.find_all(exp.Table):
            table_name = table.sql()
            if table.name in cte_names:
                source_ctes.add(table.name)
            else:
                source_tables.add(table_name)
        
        cte_schemas[cte_name] = {
            "columns": columns,
            "source_tables": sorted(source_tables),
            "source_ctes": sorted(source_ctes)
        }
    
    return cte_schemas

ast = parse_one(sql, read="bigquery")
schemas = get_cte_schemas(ast)

for cte_name, schema in schemas.items():
    print(f"\nCTE: {cte_name}")
    print("Source Tables:", schema["source_tables"])
    print("Source CTEs:", schema["source_ctes"])
    print("Columns:")
    for col_name, col_expr in schema["columns"].items():
        print(f"  {col_name}: {col_expr}")


def expand_all_stars(ast, debug=False):
    """
    Multi-pass star expansion that handles CTEs and main query recursively
    """
    max_passes = 5  # Prevent infinite loops
    for _ in range(max_passes):
        if debug:
            print(f"\n=== Starting expansion pass {_ + 1} ===")
        
        # Get current CTE schemas
        cte_schemas = get_cte_schemas(ast)
        
        # Check if there are any stars left to expand
        has_stars = any(
            isinstance(e, (exp.Star, exp.Column)) 
            for select in ast.find_all(exp.Select) 
            for e in select.args.get("expressions", [])
        )
        
        if not has_stars:
            if debug:
                print("No more stars to expand")
            break
        
        # First expand stars in CTEs (bottom-up)
        if ast.args.get("with"):
            for cte in ast.args["with"].expressions:
                if debug:
                    print(f"\nExpanding stars in CTE: {cte.alias}")
                expand_select_star(cte.this, cte_schemas, debug)
        
        # Update CTE schemas after expansion
        cte_schemas = get_cte_schemas(ast)
        
        # Then expand stars in main query
        main_select = ast.find(exp.Select)
        if main_select:
            if debug:
                print("\nExpanding stars in main query")
            expand_select_star(main_select, cte_schemas, debug)
    else:
        if debug:
            print("Warning: Reached maximum expansion passes")
    
    return ast

def expand_select_star(select, cte_schemas, debug=False):
    """
    Expanded version that works on a single SELECT statement
    """
    if debug:
        print("\nProcessing SELECT:", select.sql())
    
    # Get all source tables including JOINs
    from_source = select.args.get("from")
    from_tables = get_source_tables(from_source, cte_schemas) if from_source else []
    
    join_tables = []
    for join in select.args.get("joins", []):
        join_tables.extend(get_source_tables(join.this, cte_schemas))
    
    all_sources = from_tables + join_tables
    if debug:
        print(f"Available sources: {all_source_names(all_sources)}")
    
    new_exprs = []
    for expr in select.args.get("expressions", []):
        if isinstance(expr, exp.Star):
            if debug:
                print("Processing unqualified *")
            handle_unqualified_star(expr, all_sources, cte_schemas, new_exprs, debug)
        
        elif isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
            if debug:
                print(f"Processing qualified {expr.sql()}")
            handle_qualified_star(expr, cte_schemas, new_exprs, debug)
        
        else:
            if debug:
                print(f"Keeping existing expression: {expr.sql()}")
            new_exprs.append(expr)
    
    select.set("expressions", new_exprs)
    if debug:
        print("New expressions:", [e.sql() for e in new_exprs])

def handle_unqualified_star(star, sources, cte_schemas, new_exprs, debug):
    """Handle unqualified * expansion"""
    expanded_any = False
    for source in sources:
        if source["name"] in cte_schemas:
            if debug:
                print(f"Expanding {source['alias']}.* from CTE {source['name']}")
            expand_cte_columns(source, cte_schemas, new_exprs, debug)
            expanded_any = True
        else:
            if debug:
                print(f"Keeping {source['alias']}.* - not a CTE")
            new_exprs.append(exp.Star(**{
                "table": source["alias"],
                "except": star.args.get("except"),
                "replace": star.args.get("replace")
            }))
    
    if not expanded_any:
        new_exprs.append(star)

def handle_qualified_star(column, cte_schemas, new_exprs, debug):
    """Handle table.* expansion"""
    table_name = column.args["table"]
    if table_name in cte_schemas:
        if debug:
            print(f"Expanding {table_name}.* from CTE")
        expand_cte_columns({
            "name": table_name,
            "alias": table_name,
            "is_cte": True
        }, cte_schemas, new_exprs, debug)
    else:
        if debug:
            print(f"Keeping {table_name}.* - not a CTE")
        new_exprs.append(column)

def expand_cte_columns(source, cte_schemas, new_exprs, debug):
    """Expand columns from a CTE source"""
    for col_name in cte_schemas[source["name"]]["columns"]:
        new_col = Column(this=col_name, table=source["alias"])
        new_exprs.append(new_col)
        if debug:
            print(f"Added column: {new_col.sql()}")

def all_source_names(sources):
    """Helper for debug output"""
    return [f"{s['name']} (as {s['alias']})" for s in sources]

def get_source_tables(source, cte_schemas):
    """Handle all possible source types and return consistent table info"""
    if isinstance(source, exp.From):
        return get_source_tables(source.this, cte_schemas)
    elif isinstance(source, exp.Table):
        return [{
            "name": source.name,
            "alias": source.alias or source.name,
            "is_cte": source.name in cte_schemas
        }]
    elif isinstance(source, exp.Alias):
        if isinstance(source.this, exp.Table):
            return [{
                "name": source.this.name,
                "alias": source.alias,
                "is_cte": source.this.name in cte_schemas
            }]
        elif isinstance(source.this, exp.Subquery):
            return [{
                "name": source.alias,  # For subqueries, the alias becomes the name
                "alias": source.alias,
                "is_cte": False
            }]
    elif isinstance(source, exp.Subquery):
        return [{
            "name": source.alias,
            "alias": source.alias,
            "is_cte": False
        }] if source.alias else []
    return []


ast = parse_one(sql, read="bigquery")

print("\nRunning expansion with debug...")
expanded_ast = expand_all_stars(ast, debug=True)
print(expanded_ast.sql(pretty=True))

print("\nFinal Output:")
print(expanded_ast.sql(pretty=True))


def insert_sql(main_sql, B, C, threshold = 90):
    main_lines = main_sql.splitlines()
    main_block = "/n".join(normalize_sql(main_lines))
    B_lines = B.splitlines()
    B_block = "/n".join(normalize_sql(B_lines))

    best_score = -1
    best_start = -1

    for i in range(len(main_lines) - len(B_lines)):
        block = "/n".join(main_lines[i:i + len(B_lines)])
        block_norm = "/n".join(normalize_sql(block))
        score = fuzz.ratio(block_norm, B_block)
        if score > best_score:
            best_score = score
            best_start = i

    if best_score >= threshold:
        A_part = "/n".join(main_lines[:best_start])
        B_part = "/n".join(main_lines[best_start:])
        result = A_part + "/n" + C + "/n" + B_part

    return result

def wrap_select_with_insert(sql: str, replacements: Dict, filename: Optional[str] = None)
    for old, new in replacements.items():
        sql = sql.replace(old, new)

    formatted_sql = format_and_clean_sql(sql)
    parsed = parse_one(formatted_sql, read='bigquery')

    def find_main_select(parsed):
        main_select = None
        cte = parsed.find(exp.With)
        if cte:
            full_sql = parsed.sql()
            cte_sql = cte.sql()

            cte_start = full_sql.find(cte_sql)
            if cte_start == -1:
                return {"error : could not find CTE"}

            main_query_start = cte_start + len(cte_sql)
            main_query_sql = full_sql[main_query_start:].strip()
            if main_query_sql.endswith(";"):
                main_query_sql = main_query_sql[:-1]
        else:
            main_query_sql = parsed.find(exp.Select)

        return main_query_sql

    final_select = find_main_select(parsed)
    final_select_parsed = parse_one(final_select, read='bigquery')

    if not final_select_parsed:
        return ValueError("Could not parse the main SELECT statement.")

    output_columns = []
    for projection in final_select_parsed.expressions:
        alias = projection.alias
        name = alias or projection.name or "col"
        output_columns.append(name)

    from_expr = final_select_parsed.args.get("from")
    from_table_name = from_expr.name if from_expr.name else "unknown"

    if from_expr and from_expr.expressions:
        from_table_expr = from_expr.expressions[0]
        if isinstance(from_table_expr, sqlglot.exp.Table):
            from_table_name = from_table_expr.name
        elif isinstance(from_table_expr, sqlglot.exp.Subquery):
            from_table_name = from_table_expr.alias or "subquery"

    target_table = f"dummy_{from_table_name}"
    column_list = ", ".join(output_columns)

    insert_stmt = f"INSERT INTO {target_table} ({column_list})"
    result = insert_sql(formatted_sql, format_and_clean_sql(final_select), insert_stmt, threshold=90)

    return result

def clean_lineage_tuple(variable):
    for v in variable:
        if '<default>.' in v:
            new_variable = v.replace('<default>.', '')
        else:
            new_variable = v
    return new_variable

def extract_column_lineage(sql, replacements, modify=True):
    if modify:
        sql = wrap_select_with_insert(sql, replacements)
    
    runner = LineageRunner()
    lineage_mapping = []

    raw_lineage = list(runner.get_column_lineage())

    for lineage_tuple in raw_lineage:
        try:
            chain = [str(col) for col in lineage_tuple]
            full_path = []
            for i in range(len(chain) - 1):
                target = chain[i].split(",")
                source = chain[i + 1].split(",")
                target_tv = clean_lineage_tuple(target)
                source_tv = clean_lineage_tuple(source)

                lineage_mapping.append((
                    target_tv if len(target_tv.split('.'))==2 else f"<unknown>.{target_tv}",
                    source_tv if len(source_tv.split('.'))==2 else f"<unknown>.{source_tv}"
                ))

        except Exception as e:
            print(f"Error processing lineage tuple {lineage_tuple}: {e}")
            continue

    lineage_df = pd.DataFrame(lineage_mapping, columns=["derived", "source"])
    lineage_df[['derived_table', 'derived_column']] = lineage_df['derived'].str.split('.', expand=True)
    lineage_df[['source_table', 'source_column']] = lineage_df['source'].str.split('.', expand=True)

    lineage_df = lineage_df[['source_table', 'source_column', 'derived_table', 'derived_column']].drop_duplicates().reset_index(drop=True)

    return lineage_df

replacements = {
    '[$target_dataset].':'',
    '"[$run_type]"':'runtype',
    "'[$run_type]'":'runtype',
    '[$regulator_table]':'EBA'
}


folder_path = r"C:\Users\data\sql_files"

master_lineage_df = pd.DataFrame()
for filename in os.listdir(folder_path):
    if filename.endswith(".sql"):
        file_path = os.path.join(folder_path, filename)
        with open(file_path, 'r') as file:
            sql = file.read()
            try:
                sql_lineage_df = extract_column_lineage(sql, replacements)
                master_lineage_df = pd.concat([master_lineage_df, sql_lineage_df], ignore_index=True)
                master_lineage_df = master_lineage_df.drop_duplicates().reset_index(drop=True)
            except Exception as e:
                print(f"Error processing file {filename}: {e}")
# Example usage
if __name__ == "__main__":
    replacements = {
        '[$target_dataset].': '',
        '"[$run_type]"': 'runtype',
        "'[$run_type]'": 'runtype',
        '[$regulator_table]': 'EBA'
    }
    
    folder_path = r"C:\Users\data\sql_files"
    master_lineage_df = process_sql_folder(folder_path, replacements)
    print(f"Processed {len(master_lineage_df)} lineage records")



###############################################################

