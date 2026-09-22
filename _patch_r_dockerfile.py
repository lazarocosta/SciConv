"""Patch the Dockerfile generation prompt to add R-specific build rules."""

path = r'C:\Users\PRECISION\Desktop\SciConv\backendChat\routes\project.py'

with open(path, 'rb') as f:
    lines = f.readlines()

# Find the line with the R base image rule (line ~2400, 0-indexed ~2399)
target_prefix = b"                           '\\nIMPORTANT base image selection rules:'"
target_idx = None
for i, line in enumerate(lines):
    if line.startswith(target_prefix):
        target_idx = i
        break

if target_idx is None:
    print('ERROR: target line not found')
    exit(1)

print(f'Found target at line {target_idx+1}')

NL = b'\r\n'
BSN = b'\\n'

# New rules block replacing lines target_idx to target_idx+4 (the 4 rule lines + closing brace line)
new_rules = [
    b"                           '" + BSN + b"IMPORTANT base image selection rules:'" + NL,
    b"                           '" + BSN + b"- For R projects: ALWAYS use rocker/r-ver:<version> instead of r-base:<version>. The rocker images have properly maintained apt package sources.'" + NL,
    b"                           '" + BSN + b"- For R projects: Before installing R packages, add a RUN apt-get update -qq && apt-get install -y libssl-dev libxml2-dev libcurl4-openssl-dev libfontconfig1-dev && rm -rf /var/lib/apt/lists/* step.'" + NL,
    b"                           '" + BSN + b"- For R projects: After the install.packages() call, add a verification step: RUN Rscript -e \"pkgs <- c(<same list>); missing <- pkgs[!pkgs %in% installed.packages()[,'Package']]; if (length(missing)) stop(paste('Failed to install:', paste(missing, collapse=', ')))\"'" + NL,
    b"                           '" + BSN + b"- For Python projects: use python:<version>-slim or python:<version>.'" + NL,
    b"                           '" + BSN + b"- For Julia projects: use julia:<version>.'" + NL,
    b"                           '" + BSN + b"Provide only the created Dockerfile, as I will use your response directly, no additional text or explanation is needed.'}" + NL,
]

# The old block is 4 lines (IMPORTANT + R rule + Python rule + Julia rule + closing line = 5 lines)
old_count = 5  # lines target_idx through target_idx+4
lines[target_idx:target_idx + old_count] = new_rules

with open(path, 'wb') as f:
    f.writelines(lines)

print('Patch applied. Verifying syntax...')
