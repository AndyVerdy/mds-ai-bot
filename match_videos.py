#!/usr/bin/env python3
"""
Match transcript files to video URLs from CSV.
Uses multi-strategy matching: name-based, channel-type, date, and fuzzy.
"""

import csv
import json
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

CSV_PATH = "/Users/Born/Downloads/videos - Sheet1 (1).csv"
TRANSCRIPTS_DIR = "/Users/Born/mds-ai-bot/data/otter-export-new/Uncategorized"
OUTPUT_PATH = "/Users/Born/mds-ai-bot/data/video_links.json"

# ── Helpers ──────────────────────────────────────────────────────────

def normalize(s):
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower().strip()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def extract_date_from_filename(filename):
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_', filename)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d')
        except:
            pass
    return None

def extract_title_from_filename(filename):
    name = filename.replace('.txt', '')
    name = re.sub(r'^\d{4}-\d{2}-\d{2}_', '', name)
    return name.strip()

def parse_csv_date(date_str):
    date_str = date_str.strip()
    for fmt in ['%m-%d-%Y %H:%M', '%m-%d-%Y', '%m/%d/%Y %H:%M', '%m/%d/%Y']:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    return None

def extract_person_name_from_call_title(title):
    """Extract person/company name from '... Call with X' patterns."""
    patterns = [
        r'(?:mogul|expert|coaching)\s+call\s+(?:with|featuring)\s+(.+?)(?:\s+on\s+|\s+from\s+|\s+Part\s+\d|\s*$)',
        r'(?:mogul|expert|coaching)\s+call\s+(?:with|featuring)\s+(.+?)$',
    ]
    t = title.strip()
    for pat in patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            name = re.sub(r'\s*\(.*\)\s*$', '', name)
            name = re.sub(r'\s*-\s*$', '', name)
            return name
    
    # Also: "Mogul Call Hotseat with X"
    m = re.search(r'(?:mogul|expert)\s+call\s+\w+\s+with\s+(.+?)$', t, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    
    return None

def extract_person_from_video_title(title):
    """Extract person name from video title."""
    # Pattern 1: "... Call with Person"
    name = extract_person_name_from_call_title(title)
    if name:
        return name
    
    # Pattern 2: "Topic — Person Name — Call Type" (various dash types)
    parts = re.split(r'\s*[－–—]\s*', title)
    if len(parts) >= 3:
        last = parts[-1].lower()
        if any(w in last for w in ['mogul call', 'expert call', 'chapter', 'mastermind', 
                                    'coaching', 'inspire', 'retreat', 'dtc', 'summit',
                                    'kickstart', 'boardroom', 'bonding']):
            return parts[-2].strip()
    
    # Pattern 3: "Topic - Person Name - Call Type" (regular dash)
    parts = re.split(r'\s+-\s+', title)
    if len(parts) >= 3:
        last = parts[-1].lower()
        if any(w in last for w in ['mogul call', 'expert call', 'chapter', 'mastermind',
                                    'coaching', 'inspire', 'retreat', 'dtc', 'summit',
                                    'all stars', 'level up']):
            return parts[-2].strip()
    
    # Pattern 4: "Person Name: Topic" or "Person Name － Topic" at start
    if ':' in title:
        before_colon = title.split(':')[0].strip()
        words = before_colon.split()
        if 1 <= len(words) <= 4 and all(w[0].isupper() for w in words if w.isalpha() and len(w) > 1):
            return before_colon
    
    # Pattern 5: "Person Name − Topic − Event" at start
    if len(parts) >= 2:
        first = parts[0].strip()
        words = first.split()
        if 1 <= len(words) <= 4 and all(w[0].isupper() for w in words if w.isalpha() and len(w) > 1):
            return first
    
    return None

def names_match(name1, name2):
    """Check if two person/company names refer to the same entity."""
    if not name1 or not name2:
        return False, 0
    
    n1 = normalize(name1)
    n2 = normalize(name2)
    
    if n1 == n2:
        return True, 1.0
    
    if n1 in n2 or n2 in n1:
        return True, 0.95
    
    w1 = set(n1.split())
    w2 = set(n2.split())
    fillers = {'mds', 'with', 'the', 'and', 'from', 'of', 'a', 'an', 'on', 'at', 'in', 'for'}
    w1 = w1 - fillers
    w2 = w2 - fillers
    
    if not w1 or not w2:
        return False, 0
    
    common = w1 & w2
    if common:
        score = len(common) / max(len(w1), len(w2))
        if score >= 0.5:
            return True, score
    
    # Fuzzy comparison
    ratio = SequenceMatcher(None, n1, n2).ratio()
    if ratio > 0.82:
        return True, ratio
    
    # Last name match
    words1 = n1.split()
    words2 = n2.split()
    if len(words1) >= 2 and len(words2) >= 2:
        if words1[-1] == words2[-1] and len(words1[-1]) > 2:
            return True, 0.9
        if words1[0] == words2[0] and words1[-1] != words2[-1]:
            return False, 0
    
    return False, 0

def extract_channel_type(title):
    t = title.lower()
    channel_patterns = {
        'seo': r'seo\s+channel',
        'logistics': r'logistics\s+channel',
        'tiktok': r'tiktok\s+channel',
        'trading': r'trading\s+channel',
        'retail': r'retail\s+channel',
        'centurion': r'centurion\s+channel',
        'real_estate': r'real\s+estate\s+channel',
        'ai': r'ai\s+channel',
        'saas': r'saas\s+(operators\s+)?channel',
        'women': r'women\s+of\s+mds|wmds\s+call',
        'rockies': r'rockies?\s+chapter',
        'apac': r'apac\s+chapter',
        'large_catalog': r'large\s+catalog',
        'resellers': r'resellers?\s+channel',
    }
    for ctype, pattern in channel_patterns.items():
        if re.search(pattern, t):
            return ctype
    return None

def extract_month_year(title, file_date=None):
    month_map = {
        'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
        'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
        'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
        'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
    }
    t = title.lower()
    for mname, mnum in month_map.items():
        m = re.search(rf'\b{mname}\w*\s+(\d{{4}})\b', t)
        if m:
            return (mnum, int(m.group(1)))
    if file_date:
        return (file_date.month, file_date.year)
    return None

def date_proximity_days(d1, d2):
    if not d1 or not d2:
        return 999
    return abs((d1 - d2).days)

# ── Load Data ────────────────────────────────────────────────────────

def load_videos():
    videos = []
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = row['title'].strip()
            url = row['url'].strip()
            speakers = row.get('speakers', '').strip()
            date_str = row.get('publishingDate', '').strip()
            
            date = parse_csv_date(date_str) if date_str else None
            
            videos.append({
                'title': title,
                'url': url,
                'speakers': speakers if speakers != 'None' else '',
                'date': date,
                'title_norm': normalize(title),
                'person': extract_person_from_video_title(title),
                'channel': extract_channel_type(title),
            })
    return videos

def load_transcripts():
    files = sorted(os.listdir(TRANSCRIPTS_DIR))
    transcripts = []
    for f in files:
        if not f.endswith('.txt'):
            continue
        title = extract_title_from_filename(f)
        date = extract_date_from_filename(f)
        person = extract_person_name_from_call_title(title)
        channel = extract_channel_type(title)
        
        transcripts.append({
            'filename': f,
            'title': title,
            'date': date,
            'person': person,
            'channel': channel,
            'title_norm': normalize(title),
        })
    return transcripts

# ── Matching Strategies ──────────────────────────────────────────────

def match_by_call_person(transcript, videos):
    """For 'Mogul Call with X' / 'Expert Call with X' patterns."""
    if not transcript['person']:
        return None
    
    t_person = transcript['person']
    
    # Handle multi-person: "hasan & Dave" → check each individually
    t_persons = [p.strip() for p in re.split(r'\s*[&,]\s*', t_person) if p.strip()]
    
    best = None
    best_score = 0
    
    for v in videos:
        # For each transcript person, check if they appear in the video
        person_matched = False
        name_score = 0
        persons_found = 0
        
        for tp in t_persons:
            matched = False
            ns = 0
            
            # Check video person name
            m, s = names_match(tp, v['person'])
            if m:
                matched, ns = True, s
            
            # Check speakers field
            if not matched and v['speakers']:
                for sp in re.split(r',\s*', v['speakers']):
                    m, s = names_match(tp, sp.strip())
                    if m:
                        matched, ns = True, s
                        break
            
            # Check if person name appears anywhere in video title
            if not matched and len(tp) > 3:
                if normalize(tp) in v['title_norm']:
                    matched, ns = True, 0.85
            
            if matched:
                persons_found += 1
                name_score = max(name_score, ns)
        
        if persons_found == 0:
            continue
        
        # Require all persons to be found for multi-person transcripts
        # For single person, require that one person
        person_matched = (persons_found >= 1)
        
        if person_matched:
            # Bonus for date proximity
            date_bonus = 0
            days = date_proximity_days(transcript['date'], v['date'])
            if days <= 7:
                date_bonus = 0.15
            elif days <= 30:
                date_bonus = 0.10
            elif days <= 90:
                date_bonus = 0.05
            
            # Bonus if both have same call type
            type_bonus = 0
            t_lower = transcript['title'].lower()
            v_lower = v['title'].lower()
            if ('mogul call' in t_lower and 'mogul call' in v_lower) or \
               ('expert call' in t_lower and 'expert call' in v_lower) or \
               ('coaching call' in t_lower and 'coaching call' in v_lower):
                type_bonus = 0.1
            
            score = name_score + date_bonus + type_bonus
            if score > best_score:
                best_score = score
                best = v
    
    if best and best_score >= 0.7:
        return {'video': best, 'score': best_score, 'method': 'call_person'}
    return None

def match_by_channel_type(transcript, videos):
    """For channel calls (SEO, Logistics, TikTok, etc.)."""
    if not transcript['channel']:
        return None
    
    t_channel = transcript['channel']
    t_month = extract_month_year(transcript['title'], transcript['date'])
    
    best = None
    best_score = 0
    
    for v in videos:
        if v['channel'] != t_channel:
            continue
        
        v_month = extract_month_year(v['title'], v['date'])
        score = 0.7  # Base score for channel match
        
        if t_month and v_month and t_month == v_month:
            score += 0.3
        elif t_month and v_month and t_month[1] == v_month[1] and abs(t_month[0] - v_month[0]) <= 1:
            score += 0.15
        else:
            days = date_proximity_days(transcript['date'], v['date'])
            if days <= 14:
                score += 0.2
            elif days <= 45:
                score += 0.1
            elif days <= 90:
                score += 0.05
        
        if score > best_score:
            best_score = score
            best = v
    
    if best and best_score >= 0.85:
        return {'video': best, 'score': best_score, 'method': 'channel_type'}
    return None

def match_by_speaker_name(transcript, videos):
    """For summit talks where transcript is just a person name."""
    title = transcript['title']
    t_lower = title.lower()
    
    # Skip if it looks like a call/channel/event title
    skip_words = ['call', 'channel', 'meeting', 'keynote', 'hack', 'contest',
                  'q4', 'q5', 'q6', 'q7', 'q8', 'q9', 'q10',
                  'fc chats', 'recording', 'reel', 'event', 'huddle',
                  'post-event', 'takeaway', 'town hall', 'trend report',
                  'notes', 'community', 'different', 'networking',
                  'videoplayback', 'product launching', 'logistics',
                  'system', 'hiring', 'tiktok', 'inspire', 'amazon at',
                  'crossover', 'how mds']
    if any(w in t_lower for w in skip_words):
        return None
    
    # Clean up
    clean_title = re.sub(r'^\d+\.\s*', '', title).strip()
    clean_title = re.sub(r'[\s_]+\d+$', '', clean_title)
    clean_title = re.sub(r'_\d+$', '', clean_title)
    clean_title = re.sub(r'\s*\(.*?\)\s*$', '', clean_title)
    clean_title = re.sub(r'_no Q&A$', '', clean_title)
    clean_title = clean_title.strip()
    
    # Skip very short or clearly non-name titles
    words = clean_title.split()
    if len(words) < 1 or len(words) > 5:
        return None
    if clean_title.lower() in ['logistics', 'tiktok', 'system & hiring', 'product launching']:
        return None
    
    # Handle multi-person: "Alex Chiru & Morris Sued"
    persons = [p.strip() for p in re.split(r'\s*[&]\s*', clean_title) if p.strip()]
    
    candidates = []
    
    for v in videos:
        total_matched = 0
        total_score = 0
        
        for person in persons:
            matched = False
            ns = 0
            
            # Priority 1: Name in video TITLE (preferred)
            m, s = names_match(person, v['person'])
            if m:
                matched, ns = True, s + 0.05  # Small bonus for title match
            
            if not matched and len(person) > 3:
                if normalize(person) in v['title_norm']:
                    matched, ns = True, 0.85
            
            # Priority 2: Name in speakers field (less preferred)
            if not matched and v['speakers']:
                for sp in re.split(r',\s*', v['speakers']):
                    m, s = names_match(person, sp.strip())
                    if m:
                        matched, ns = True, s - 0.05  # Small penalty for speakers-only match
                        break
            
            if matched:
                total_matched += 1
                total_score += ns
        
        if total_matched == 0:
            continue
        
        name_score = total_score / len(persons)
        
        # Date proximity bonus
        days = date_proximity_days(transcript['date'], v['date'])
        date_bonus = 0
        if days <= 14:
            date_bonus = 0.15
        elif days <= 60:
            date_bonus = 0.08
        elif days <= 180:
            date_bonus = 0.03
        
        final_score = name_score + date_bonus
        candidates.append((v, final_score))
    
    if not candidates:
        return None
    
    # Sort by score and pick best
    candidates.sort(key=lambda x: x[1], reverse=True)
    best_v, best_score = candidates[0]
    
    if best_score >= 0.7:
        return {'video': best_v, 'score': best_score, 'method': 'speaker_name'}
    return None

def match_by_fc_chats(transcript, videos):
    """For FC Chats transcripts."""
    title = transcript['title']
    if 'fc chats' not in title.lower():
        return None
    
    # Extract subtitle: handle both "FC Chats: ..." and "FC Chats_ ..."
    m = re.search(r'FC Chats[_:\s]+(.+)', title, re.IGNORECASE)
    if not m:
        return None
    subtitle = m.group(1).strip()
    sub_norm = normalize(subtitle)
    
    best = None
    best_score = 0
    
    for v in videos:
        v_lower = v['title'].lower()
        # FC Chats in video might use different format
        if 'fc chat' not in v_lower and 'founders club' not in v_lower:
            continue
        
        # Compare normalized full titles
        ratio = SequenceMatcher(None, normalize(title), v['title_norm']).ratio()
        
        if ratio > best_score:
            best_score = ratio
            best = v
    
    if not best:
        # Try matching subtitle content against all video titles
        for v in videos:
            ratio = SequenceMatcher(None, sub_norm, v['title_norm']).ratio()
            if ratio > best_score and ratio > 0.5:
                best_score = ratio
                best = v
    
    if best and best_score >= 0.5:
        return {'video': best, 'score': best_score, 'method': 'fc_chats'}
    return None

def match_by_exact_title(transcript, videos):
    """High-confidence fuzzy match on full title."""
    t_norm = transcript['title_norm']
    
    best = None
    best_score = 0
    
    for v in videos:
        ratio = SequenceMatcher(None, t_norm, v['title_norm']).ratio()
        
        days = date_proximity_days(transcript['date'], v['date'])
        date_bonus = 0
        if days <= 7:
            date_bonus = 0.1
        elif days <= 30:
            date_bonus = 0.05
        
        score = ratio + date_bonus
        if score > best_score:
            best_score = score
            best = v
    
    if best and best_score >= 0.88:
        return {'video': best, 'score': best_score, 'method': 'exact_title'}
    return None

# Special case matchers for known hard patterns
def match_special_cases(transcript, videos):
    """Handle known special patterns that don't fit other strategies."""
    title = transcript['title']
    t_lower = title.lower()
    
    # "WMDS Call Dec 2025" → Women of MDS
    if 'wmds' in t_lower:
        for v in videos:
            if 'women' in v['title'].lower() and 'mds' in v['title'].lower():
                days = date_proximity_days(transcript['date'], v['date'])
                if days <= 30:
                    return {'video': v, 'score': 0.9, 'method': 'special_case'}
    
    # "Centurion Dec 2025" → Centurion Channel Call December 2025
    if 'centurion' in t_lower and 'channel' not in t_lower:
        for v in videos:
            if 'centurion' in v['title'].lower():
                days = date_proximity_days(transcript['date'], v['date'])
                if days <= 30:
                    return {'video': v, 'score': 0.9, 'method': 'special_case'}
    
    # "Mogul Call - AI Channel Kickoff"
    if 'ai channel' in t_lower:
        for v in videos:
            if 'ai channel' in v['title'].lower():
                days = date_proximity_days(transcript['date'], v['date'])
                if days <= 30:
                    return {'video': v, 'score': 0.9, 'method': 'special_case'}
    
    return None

# ── Main ─────────────────────────────────────────────────────────────

def main():
    print("Loading videos from CSV...")
    videos = load_videos()
    print(f"  Loaded {len(videos)} videos ({sum(1 for v in videos if v['date'])} with parsed dates)")
    
    print("Loading transcripts...")
    transcripts = load_transcripts()
    print(f"  Loaded {len(transcripts)} transcripts")
    
    results = {}
    matched = 0
    unmatched = []
    methods = {}
    
    for t in transcripts:
        result = None
        
        # Try strategies in priority order
        for strategy in [match_by_fc_chats, match_by_call_person, match_by_channel_type,
                         match_by_speaker_name, match_special_cases, match_by_exact_title]:
            result = strategy(t, videos)
            if result:
                break
        
        if result:
            video = result['video']
            results[t['filename']] = {
                'video_url': video['url'],
                'video_title': video['title'],
                'match_score': round(result['score'], 3),
                'match_method': result['method'],
            }
            matched += 1
            method = result['method']
            methods[method] = methods.get(method, 0) + 1
        else:
            unmatched.append(t['filename'])
    
    # Save results
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\n{'='*60}")
    print(f"RESULTS: {matched}/{len(transcripts)} matched ({matched/len(transcripts)*100:.1f}%)")
    print(f"Methods: {json.dumps(methods, indent=2)}")
    print(f"Unmatched: {len(unmatched)}")
    print(f"\nSaved to {OUTPUT_PATH}")
    
    # Verify known matches
    print(f"\n{'='*60}")
    print("VERIFICATION - checking known tricky matches:")
    checks = [
        '2025-01-10_Mogul Call with hasan & Dave.txt',
        '2025-01-15_Mogul Call with Alina Vlaic.txt',
        '2025-01-23_Mogul Call with Rafay Hussain.txt',
        '2025-02-05_Mogul Call with Ken Freeman.txt',
        '2025-04-02_Shinghi Detlefsen.txt',
        '2025-05-28_Mogul Call with Dorian Gorsky.txt',
        '2025-06-04_MDS Mogul Call with Dorian Gorsky Part 2.txt',
        '2025-10-22_MDS Mogul Call with Dima Kubrak.txt',
        '2025-11-13_Mogul Call with Josh Hadley.txt',
        '2026-02-18_MDS Mogul Call Hotseat with Leslie Eisen.txt',
    ]
    for fname in checks:
        if fname in results:
            info = results[fname]
            print(f"  ✓ {fname}")
            print(f"    → {info['video_title']}")
            print(f"      Score: {info['match_score']} | Method: {info['match_method']}")
        else:
            print(f"  ✗ {fname} — UNMATCHED")
    
    print(f"\n{'='*60}")
    print(f"UNMATCHED FILES ({len(unmatched)}):")
    for f in sorted(unmatched):
        print(f"  {f}")

if __name__ == '__main__':
    main()
