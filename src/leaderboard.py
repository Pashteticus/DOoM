import yaml
from typing import List, Dict, Any, Tuple, Set
import time
from pathlib import Path
import json
from datetime import datetime
from src.equality_checker import MathEqualityChecker
from src.sampler import OaiSampler
from src.mat_boy import RussianMathEval
from src.types import SingleEvalResult
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from tqdm import tqdm
import hashlib
import signal
import sys

class Leaderboard:
    def __init__(self, config_path: str, output_dir: str = "results", max_workers: int = 4):
        self.config_path = config_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        
        # Создаем директории
        self.details_dir = self.output_dir / "details"
        self.details_dir.mkdir(exist_ok=True)
        self.cache_dir = self.output_dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        
        # Загружаем конфиг и кэш
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        self.model_links = self.config.get('model_links', {})
        self.equality_checker = MathEqualityChecker()
        self.results_file = self.output_dir / "leaderboard_results.json"
        self.results = self._load_results()
        
    def _get_cache_key(self, model_name: str, system_prompt: str | None) -> str:
        """Генерирует ключ кэша на основе модели и промпта"""
        # Используем безопасное имя модели для кэша
        safe_model_name = model_name.replace('/', '_')
        cache_data = {
            'model_name': safe_model_name,
            'system_prompt': system_prompt,
            'num_examples': self.config.get('num_examples'),
            'temperature': self.config.get('temperature'),
            'max_tokens': self.config.get('max_tokens'),
        }
        cache_str = json.dumps(cache_data, sort_keys=True)
        return hashlib.md5(cache_str.encode()).hexdigest()

    def _get_cached_result(self, cache_key: str) -> Dict | None:
        """Получает результат из кэша если он есть"""
        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
        return None

    def _save_to_cache(self, cache_key: str, result: Dict):
        """Сохраняет результат в кэш"""
        cache_file = self.cache_dir / f"{cache_key}.json"
        with open(cache_file, 'w') as f:
            json.dump(result, f, indent=2)

    def _load_results(self) -> Dict:
        """Загружает существующие результаты и кэш"""
        results = {}
        
        # Загружаем все результаты из кэша
        if self.cache_dir.exists():
            for cache_file in self.cache_dir.glob("*.json"):
                with open(cache_file, 'r') as f:
                    cached_result = json.load(f)
                    model_name = cached_result['model_name']
                    timestamp = cached_result['timestamp']
                    results[f"{model_name}_{timestamp}"] = cached_result
        
        # Если есть файл с результатами, добавляем их тоже
        if self.results_file.exists():
            with open(self.results_file, 'r') as f:
                file_results = json.load(f)
                results.update(file_results)
                
        return results

    def _save_results(self):
        """Сохраняет все результаты"""
        # Сохраняем в основной файл результатов
        with open(self.results_file, 'w') as f:
            json.dump(self.results, f, indent=2)

    def _save_detailed_results(self, model_name: str, results: List[SingleEvalResult], timestamp: str):
        """Сохраняет детальные результаты для модели"""
        # Создаем безопасное имя директории
        safe_model_name = model_name.replace('/', '_')
        model_dir = self.details_dir / safe_model_name
        model_dir.mkdir(exist_ok=True)
        
        # Сохраняем результаты
        details_file = model_dir / f"details_{timestamp}.json"
        with open(details_file, 'w') as f:
            json.dump(results, f, indent=2, default=lambda x: x.__dict__)

    def evaluate_model(self, model_name: str, system_prompt: str = None) -> Dict[str, Any]:
        """Оценивает одну модель"""
        cache_key = self._get_cache_key(model_name, system_prompt)
        cached_result = self._get_cached_result(cache_key)
        
        if cached_result is not None:
            if self.config.get('debug'):
                print(f"\nUsing cached result for {model_name}")
            return cached_result

        if self.config.get('debug'):
            print(f"\nEvaluating {model_name}")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_model_name = model_name.replace('/', '_')
        
        # Создаем временный конфиг
        temp_config = self.config.copy()
        temp_config['model_list'] = [model_name]
        if system_prompt is not None:
            temp_config[model_name]['system_prompt'] = system_prompt
            
        temp_config_path = self.output_dir / f"temp_config_{safe_model_name}.yaml"
        with open(temp_config_path, 'w') as f:
            yaml.dump(temp_config, f)
            
        try:
            sampler = OaiSampler(str(temp_config_path))
            evaluator = RussianMathEval(
                equality_checker=self.equality_checker,
                num_examples=self.config.get('num_examples', None),
                debug=self.config.get('debug', False)
            )
            
            start_time = time.time()
            results = evaluator(sampler)
            evaluation_time = time.time() - start_time
            
            # Сохраняем детальные результаты
            self._save_detailed_results(model_name, results.results, timestamp)
            
            total_tokens = sum(r.tokens for r in results.results if hasattr(r, 'tokens'))
            
            model_result = {
                "model_name": model_name,  # Сохраняем оригинальное имя
                "score": results.score,
                "total_tokens": total_tokens,
                "evaluation_time": evaluation_time,
                "system_prompt": system_prompt,
                "timestamp": timestamp,
                "cache_key": cache_key
            }
            
            # Сохраняем в кэш
            self._save_to_cache(cache_key, model_result)
            
            # Используем оригинальное имя модели для ключа результатов
            self.results[f"{model_name}_{timestamp}"] = model_result
            self._save_results()
            
            return model_result
            
        finally:
            temp_config_path.unlink(missing_ok=True)

    def evaluate_model_parallel(self, args: tuple) -> Dict[str, Any]:
        """Оценивает одну модель (для использования в ThreadPoolExecutor)"""
        model_name, system_prompt = args
        return self.evaluate_model(model_name, system_prompt)

    def _get_measured_models(self) -> Set[str]:
        """Получает список уже измеренных моделей из кэша"""
        measured_models = set()
        if self.cache_dir.exists():
            for cache_file in self.cache_dir.glob("*.json"):
                with open(cache_file, 'r') as f:
                    cached_data = json.load(f)
                    measured_models.add(cached_data['model_name'])
        return measured_models

    def evaluate_all_models(self, system_prompts: Dict[str, str] = None) -> None:
        """Оценивает все модели из конфига параллельно с использованием кэша"""
        if system_prompts is None:
            system_prompts = {}
            
        # Получаем список уже измеренных моделей
        measured_models = self._get_measured_models()
        
        # Получаем список всех моделей из конфига
        config_models = set(self.config['model_list'])
        
        # Находим новые модели
        new_models = config_models - measured_models
        
        if new_models:
            print(f"\nFound new models to evaluate: {', '.join(new_models)}")
        
        # Загружаем существующие кэши для всех моделей
        for model_name in config_models:
            if model_name in measured_models:
                # Загружаем кэш для существующей модели
                for cache_file in self.cache_dir.glob("*.json"):
                    with open(cache_file, 'r') as f:
                        cached_data = json.load(f)
                        if cached_data['model_name'] == model_name:
                            key = f"{model_name}_{cached_data['timestamp']}"
                            self.results[key] = cached_data
                            break
        
        # Оцениваем только новые модели
        if new_models:
            uncached_args = [
                (model_name, system_prompts.get(model_name))
                for model_name in new_models
            ]
            
            print(f"\nEvaluating {len(uncached_args)} new models...")
            
            def handle_sigint(signum, frame):
                print("\nGracefully shutting down... Please wait for current evaluations to complete.")
                executor.shutdown(wait=True)
                sys.exit(0)
            
            original_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, handle_sigint)
            
            try:
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = []
                    for args in uncached_args:
                        future = executor.submit(self.evaluate_model_parallel, args)
                        futures.append(future)
                    
                    for future in tqdm(
                        futures,
                        total=len(uncached_args),
                        desc="Evaluating new models"
                    ):
                        try:
                            result = future.result(timeout=300)
                            if result:
                                key = f"{result['model_name']}_{result['timestamp']}"
                                self.results[key] = result
                                # Сразу сохраняем результат в кэш
                                self._save_to_cache(self._get_cache_key(result['model_name'], 
                                                                      result.get('system_prompt')), 
                                                  result)
                        except TimeoutError:
                            print(f"\nWarning: Evaluation timed out for one of the models")
                        except Exception as e:
                            print(f"\nError during evaluation: {str(e)}")
            
            finally:
                signal.signal(signal.SIGINT, original_sigint)
                self._save_results()
        else:
            print("\nNo new models to evaluate, using cached results")
        
        # Проверяем, что все модели из конфига присутствуют в результатах
        missing_models = config_models - set(result['model_name'] 
                                           for result in self.results.values())
        if missing_models:
            print(f"\nWarning: Missing results for models: {', '.join(missing_models)}")
        
        self._save_results()

    def generate_markdown(self) -> str:
        """Генерирует markdown с результатами"""
        md = "# Math Evaluation Leaderboard\n\n"
        md += f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        # Заголовок таблицы
        md += "| Model | Score | Tokens Used | System Prompt | Evaluation Time | Details | Model Info |\n"
        md += "|-------|--------|-------------|---------------|----------------|----------|------------|\n"
        
        # Группируем результаты по моделям и берем лучший результат для каждой
        model_best_results = {}
        for result in self.results.values():
            model_name = result['model_name']
            if (model_name not in model_best_results or 
                result['score'] > model_best_results[model_name]['score']):
                model_best_results[model_name] = result
        
        # Сортируем результаты по score
        sorted_results = sorted(
            model_best_results.values(),
            key=lambda x: x['score'],
            reverse=True
        )
        
        # Добавляем строки таблицы
        for result in sorted_results:
            model_name = result['model_name']
            system_prompt = result['system_prompt'] or 'None'
            if len(system_prompt) > 30:
                system_prompt = system_prompt[:27] + "..."
                
            details_link = f"[Details](details/{model_name}/details_{result['timestamp']}.md)"
            
            # Добавляем ссылку на документацию модели если она есть
            model_info = ""
            if model_name in self.model_links:
                model_info = f"[📚]({self.model_links[model_name]})"
                
            md += f"| {model_name} "
            md += f"| {result['score']:.3f} "
            md += f"| {result.get('total_tokens', 0)} "
            md += f"| {system_prompt} "
            md += f"| {result['evaluation_time']:.1f}s "
            md += f"| {details_link} "
            md += f"| {model_info} |\n"
            
        # Сохраняем markdown
        with open(self.output_dir / "leaderboard.md", 'w') as f:
            f.write(md)
            
        return md

def main():
    # Пример использования
    leaderboard = Leaderboard('configs/run.yaml')
    
    # Определяем разные system prompts для моделей
    system_prompts = {
        "gpt-4-1106-preview": "You are a helpful math assistant. Answer in Russian.",
        "gpt-3.5-turbo-0125": "Solve math problems step by step. Answer in Russian.",
        "gpt-4o-mini": "You are a math expert. Provide detailed solutions in Russian.",
    }
    
    # Оцениваем все модели с разными system prompts
    leaderboard.evaluate_all_models(system_prompts)
    
    # Генерируем markdown с результатами
    md = leaderboard.generate_markdown()
    print("Leaderboard generated!")
    print(md)

if __name__ == "__main__":
    main()
